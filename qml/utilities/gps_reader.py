#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gps_reader.py -- Read a GPS fix on Sailfish OS (geoclue) for harbour-find-my-device.

Distilled from the GPS spike. Confirmed on a Fairphone 4 / Sailfish OS: a fix is
obtained from the geoclue hybris provider on the USER SESSION bus.

KEY ARCHITECTURE FACT
  * ENABLING location needs USER SESSION bus  -> see location_control.py
  * READING a position uses the USER SESSION bus (geoclue runs in the user
    session). A root system-service CANNOT reach it directly.

Provider (introspected):
  name org.freedesktop.Geoclue.Providers.Hybris
  path /org/freedesktop/Geoclue/Providers/Hybris
  ifaces org.freedesktop.Geoclue (AddReference/RemoveReference/GetStatus),
         org.freedesktop.Geoclue.Position (GetPosition, PositionChanged),
         org.freedesktop.Geoclue.Velocity (GetVelocity)

REQUIREMENTS: python3-dbus, python3-gobject.

LIBRARY USE:
    from gps_reader import get_fix, read_battery_level
    fix = get_fix(timeout=120)
    if fix.success:
        store(fix.lat, fix.lon, fix.accuracy_h, read_battery_level(), ...)
"""

import argparse
import collections
import glob
import logging
import os
import sys
import time

import dbus
import dbus.bus
import dbus.mainloop.glib
from gi.repository import GLib

log = logging.getLogger("fmd.gps")

PROVIDER_NAME = "org.freedesktop.Geoclue.Providers.Hybris"
PROVIDER_PATH = "/org/freedesktop/Geoclue/Providers/Hybris"
IFACE_GEOCLUE = "org.freedesktop.Geoclue"
IFACE_POSITION = "org.freedesktop.Geoclue.Position"
IFACE_VELOCITY = "org.freedesktop.Geoclue.Velocity"

F_LAT = 1 << 0
F_LON = 1 << 1
F_ALT = 1 << 2
V_SPEED = 1 << 0
STATUS = {0: "error", 1: "unavailable", 2: "acquiring", 3: "available"}

FixResult = collections.namedtuple(
    "FixResult",
    "success lat lon alt accuracy_h speed timestamp_utc timestamp_local error")


def iso_utc(t=None):
    t = t if t is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def iso_local(t=None):
    t = t if t is not None else time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
    off = -time.timezone if (time.localtime(t).tm_isdst == 0) else -time.altzone
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return "{}{}{:02d}:{:02d}".format(base, sign, off // 3600, (off % 3600) // 60)


def make_session_bus(mainloop=None):
    """Connect to the user session bus, auto-locating it when run as root."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not addr:
        socks = sorted(glob.glob("/run/user/*/dbus/user_bus_socket")) \
            or sorted(glob.glob("/run/user/*/bus"))
        addr = "unix:path=" + socks[0] if socks else \
            "unix:path=/run/user/100000/dbus/user_bus_socket"
        log.info("attaching to user session bus: %s", addr)
    if mainloop is not None:
        return dbus.bus.BusConnection(addr, mainloop=mainloop)
    return dbus.bus.BusConnection(addr)


def read_battery_level():
    """Best-effort battery percentage (0-100) or None."""
    candidates = ["/run/state/namespaces/Battery/ChargePercentage"]
    candidates += sorted(glob.glob("/sys/class/power_supply/*/capacity"))
    for p in candidates:
        try:
            with open(p) as fh:
                return int(float(fh.read().strip()))
        except Exception:
            continue
    return None


def _drive_until_fix(loop, best, min_accuracy, timeout, should_abort=None):
    #Run the GLib loop without MainLoop.run(), so it also works off the main thread.
    context = loop.get_context()
    deadline = time.time() + timeout + 2.0
    while time.time() < deadline:
        if should_abort is not None and should_abort():
            break
        context.iteration(True)  # blocks until the next source (poll fires <=2s)
        r = best.get("r")
        if r is None:
            continue
        if min_accuracy is None:
            break
        if r.accuracy_h is not None and r.accuracy_h <= min_accuracy:
            break


def get_fix(timeout=120.0, min_accuracy=None, bus=None, should_abort=None):
    """Obtain one GPS fix.

    timeout      : seconds to wait for a (good enough) fix.
    min_accuracy : if set, keep waiting until horizontal accuracy <= this many
                   metres (or timeout, then return the best seen).
    should_abort : optional callable; when it returns True the wait ends early
                   (checked between loop iterations, so within ~2s).
    """
    own_bus = bus is None
    mainloop = dbus.mainloop.glib.DBusGMainLoop()
    try:
        bus = bus or make_session_bus(mainloop=mainloop)
        obj = bus.get_object(PROVIDER_NAME, PROVIDER_PATH)
    except Exception as exc:
        return FixResult(False, None, None, None, None, None, None, None,
                         "cannot reach geoclue provider: %s" % exc)

    geo = dbus.Interface(obj, IFACE_GEOCLUE)
    pos = dbus.Interface(obj, IFACE_POSITION)
    try:
        geo.AddReference()
    except Exception as exc:
        log.warning("AddReference failed: %s", exc)

    loop = GLib.MainLoop()
    best = {"r": None}

    def consider(fields, lat, lon, alt, acc):
        fields = int(fields)
        if not (fields & (F_LAT | F_LON)):
            return
        acc_h = float(acc[1]) if acc and len(acc) >= 2 else None
        t = time.time()
        speed = None
        try:
            vf, vts, vspeed, vdir, vclimb = dbus.Interface(
                obj, IFACE_VELOCITY).GetVelocity()
            if int(vf) & V_SPEED:
                speed = float(vspeed)
        except Exception:
            pass
        r = FixResult(True, float(lat), float(lon),
                      float(alt) if fields & F_ALT else None,
                      acc_h, speed, iso_utc(t), iso_local(t), None)
        best["r"] = r
        if min_accuracy is None or (acc_h is not None and acc_h <= min_accuracy):
            GLib.timeout_add(50, loop.quit)

    bus.add_signal_receiver(
        lambda fields, ts, lat, lon, alt, acc: consider(fields, lat, lon, alt, acc),
        dbus_interface=IFACE_POSITION, signal_name="PositionChanged")

    def poll():
        if best["r"] and (min_accuracy is None):
            return False
        try:
            fields, ts, lat, lon, alt, acc = pos.GetPosition()
            consider(fields, lat, lon, alt, acc)
        except Exception as exc:
            log.debug("GetPosition: %s", exc)
        return True

    GLib.timeout_add(2000, poll)
    GLib.timeout_add(int(timeout * 1000), lambda: (loop.quit(), False)[-1])
    GLib.timeout_add(50, poll)  # try immediately (often a cached fix)

    try:
        _drive_until_fix(loop, best, min_accuracy, timeout, should_abort)
    finally:
        try:
            geo.RemoveReference()
        except Exception:
            pass

    if best["r"]:
        r = best["r"]
        log.info("fix: lat=%.6f lon=%.6f acc=%sm", r.lat, r.lon, r.accuracy_h)
        return r
    return FixResult(False, None, None, None, None, None, None, None,
                     "no fix within %ss" % timeout)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Read one GPS fix (geoclue).")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--min-accuracy", type=float, default=None,
                    help="wait for horizontal accuracy <= N metres")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    fix = get_fix(timeout=args.timeout, min_accuracy=args.min_accuracy)
    bat = read_battery_level()
    if fix.success:
        print("RESULT success=1 lat={:.6f} lon={:.6f} alt={} acc_h={} speed={} "
              "battery={} ts_utc={} ts_local={}".format(
                  fix.lat, fix.lon, fix.alt, fix.accuracy_h, fix.speed,
                  bat, fix.timestamp_utc, fix.timestamp_local))
        return 0
    print("RESULT success=0 error={} battery={}".format(fix.error, bat))
    return 1


if __name__ == "__main__":
    sys.exit(_main())
