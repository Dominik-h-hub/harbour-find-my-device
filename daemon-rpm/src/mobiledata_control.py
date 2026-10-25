#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mobiledata_control.py -- Temporarily switch on mobile data (Sailfish OS).

  * The "Mobile data" switch in Settings / the top menu changes exactly one
    thing: AutoConnect on the ConnMan cellular service of the data SIM
    (/net/connman/service/cellular_<IMSI>_context1). ConnMan then connects or
    disconnects on its own (online in ~0.5-1 s, offline at once).
  * The normal user may set AutoConnect -- no root/priv service needed.
    Connect()/Disconnect() (PermissionDenied) and technology/ofono "Powered"
    (PermissionDenied / AccessDenied, and ofono leaves the UI out of sync)
    are not used.
  * Dual SIM: org.nemomobile.ofono.ModemManager.GetAll() element 3 is the IMSI
    of the data SIM; the service path contains it.

MobileData switch will be turned on only:
  * only if the user opted in (setting auto_enable_mobile_data),
  * never while WLAN is connected (then there is a connection anyway),
  * never while roaming (costs),
  * only if mobile data is off -- and then switched off again afterwards; a
    connection the user turned on is never touched.

USAGE:
    with mobiledata_control.temporarily_enabled(allowed) as md:
        ... upload / publish ...
    md.note   # one line for the notification, or None

CLI (testing on the device):
    python3 mobiledata_control.py status
    python3 mobiledata_control.py cycle      # enable, wait, restore
"""

import argparse
import contextlib
import errno
import fcntl
import logging
import os
import sys
import time

log = logging.getLogger("fmd.mobiledata")

CONNMAN = "net.connman"
CM_MANAGER = "net.connman.Manager"
CM_SERVICE = "net.connman.Service"
OFONO = "org.ofono"
NEMO_MODEM_MANAGER = "org.nemomobile.ofono.ModemManager"
# Index of the default data SIM's IMSI in ModemManager.GetAll() (spike result).
_GETALL_DATA_IMSI = 3

ONLINE_TIMEOUT_S = 30
# Both daemons (cmd + gps) may want mobile data at the same time. Without a
# common lock the second one would see AutoConnect=True, treat it as "the
# user's", and the first one would switch it off under its feet. The lock
# serializes the enable..restore window across the two processes.
LOCK_TIMEOUT_S = 180
_LOCK_NAME = "harbour-find-my-device-mobiledata.lock"


class Session(object):
    """Outcome of temporarily_enabled(); `note` is meant for notifications."""

    def __init__(self):
        self.enabled_by_us = False
        self.service = None
        self.note = None
        self.online = None


# --------------------------------------------------------------------------
# D-Bus helpers
def _bus():
    import dbus
    return dbus.SystemBus()


def _manager(bus):
    import dbus
    return dbus.Interface(bus.get_object(CONNMAN, "/"), CM_MANAGER)


def _services(bus, kind):
    return [(str(path), props) for path, props in _manager(bus).GetServices()
            if str(props.get("Type", "")) == kind]


def wifi_connected(bus=None):
    bus = bus or _bus()
    return any(str(p.get("State", "")) in ("ready", "online")
               for _path, p in _services(bus, "wifi"))


def _data_imsi(bus):
    import dbus
    try:
        mm = dbus.Interface(bus.get_object(OFONO, "/"), NEMO_MODEM_MANAGER)
        values = mm.GetAll()
        return str(values[_GETALL_DATA_IMSI]) or None
    except Exception as exc:
        log.info("data SIM not determinable (%s)", exc)
        return None


def data_service(bus=None):
    """(path, props) of the ConnMan cellular service of the data SIM, or
    (None, None). With a single cellular service that one is used."""
    bus = bus or _bus()
    services = _services(bus, "cellular")
    imsi = _data_imsi(bus)
    if imsi:
        for path, props in services:
            if "cellular_%s_" % imsi in path:
                return path, props
    if len(services) == 1:
        return services[0]
    log.warning("no cellular service for data SIM %s among %s",
                imsi, [p for p, _ in services])
    return None, None


def _service_props(bus, path):
    import dbus
    return dbus.Interface(bus.get_object(CONNMAN, path),
                          CM_SERVICE).GetProperties()


def _set_autoconnect(bus, path, value):
    import dbus
    dbus.Interface(bus.get_object(CONNMAN, path), CM_SERVICE).SetProperty(
        "AutoConnect", dbus.Boolean(value))


def _wait_online(bus, path, timeout):
    """Wait until the service is online. 'ready' (connected, internet check
    not finished) at the deadline still counts as usable."""
    deadline = time.monotonic() + timeout
    state = None
    while True:
        try:
            state = str(_service_props(bus, path).get("State", ""))
        except Exception as exc:
            log.warning("cannot read %s state: %s", path, exc)
        if state == "online":
            return True
        if time.monotonic() >= deadline:
            return state == "ready"
        time.sleep(0.5)


# --------------------------------------------------------------------------
# cross-process lock
def _lock_path():
    base = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    if not os.path.isdir(base):
        base = "/tmp"
    return os.path.join(base, _LOCK_NAME)


@contextlib.contextmanager
def _exclusive(timeout=LOCK_TIMEOUT_S):
    fh = open(_lock_path(), "a")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("mobile data lock busy")
                time.sleep(0.5)
        yield
    finally:
        fh.close()   # releases the lock


# --------------------------------------------------------------------------
# public API
def enable_if_needed(allowed, timeout=ONLINE_TIMEOUT_S, bus=None):
    """Switch mobile data on if policy allows and it is off. Returns a Session;
    pass it to restore() afterwards. Never raises."""
    s = Session()
    if not allowed:
        return s
    try:
        bus = bus or _bus()
        if wifi_connected(bus):
            log.info("mobile data: WLAN connected, nothing to do")
            return s
        path, props = data_service(bus)
        if path is None:
            s.note = "Mobile data not available"
            return s
        s.service = path
        if bool(props.get("AutoConnect", False)):
            log.info("mobile data: already on (%s)", path)
            return s
        if bool(props.get("Roaming", False)):
            log.info("mobile data: off and roaming, not enabling (%s)", path)
            s.note = "Mobile data is off (roaming, not enabled)"
            return s
        log.info("mobile data: off; enabling temporarily (%s)", path)
        t0 = time.monotonic()
        _set_autoconnect(bus, path, True)
        s.enabled_by_us = True
        s.online = _wait_online(bus, path, timeout)
        log.info("mobile data: %s after %.1fs",
                 "online" if s.online else "NOT online", time.monotonic() - t0)
        s.note = ("Mobile data was off, enabled temporarily" if s.online
                  else "Mobile data was off, enabled but no connection")
    except Exception as exc:
        log.error("mobile data: enabling failed: %s", exc)
        s.note = "Mobile data could not be enabled"
    return s


def restore(session, bus=None):
    """Switch mobile data off again if enable_if_needed() switched it on."""
    if not session or not session.enabled_by_us:
        return
    try:
        _set_autoconnect(bus or _bus(), session.service, False)
        session.enabled_by_us = False
        log.info("mobile data: switched off again (%s)", session.service)
    except Exception as exc:
        log.error("mobile data: could not switch off again (%s): %s",
                  session.service, exc)


@contextlib.contextmanager
def temporarily_enabled(allowed, timeout=ONLINE_TIMEOUT_S):
    """Context manager around a network action; see module doc. Yields the
    Session; mobile data is restored on exit, also on exceptions."""
    if not allowed:
        yield Session()
        return
    try:
        lock = _exclusive()
        lock.__enter__()
    except Exception as exc:
        log.warning("mobile data: lock unavailable (%s); not enabling", exc)
        yield Session()
        return
    session = None
    try:
        session = enable_if_needed(True, timeout)
        yield session
    finally:
        restore(session)
        lock.__exit__(None, None, None)


# --------------------------------------------------------------------------
def _main(argv=None):
    ap = argparse.ArgumentParser(description="FMD mobile data control.")
    ap.add_argument("action", choices=["status", "cycle"])
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bus = _bus()
    if args.action == "status":
        path, props = data_service(bus)
        log.info("WLAN connected: %s", wifi_connected(bus))
        log.info("data service: %s", path)
        if props:
            log.info("  State=%s AutoConnect=%s Roaming=%s",
                     props.get("State"), bool(props.get("AutoConnect")),
                     bool(props.get("Roaming")))
        return 0
    with temporarily_enabled(True) as s:
        log.info("session: enabled_by_us=%s online=%s note=%s",
                 s.enabled_by_us, s.online, s.note)
        time.sleep(3)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
