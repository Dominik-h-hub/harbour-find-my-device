#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sms_sender.py -- Outgoing SMS for harbour-find-my-device (Sailfish OS).

Confirmed on a Fairphone 4 / Sailfish OS: org.ofono.MessageManager.SendMessage
on modem /ril_0 sends an SMS and returns the message object path.

Used by the GPS remote command to reply with coordinates by SMS. The companion
receiver is sms_command_listener.py; secret/DB handling stays in the daemon.

IMPORTANT: SMS will be sent directly by modem, NO commhistory will be saved here,
so sent SMS will not be shown in sent sms messages. To mitigate this the notification
to SFOS devices shows that an SMS was sent.

REQUIREMENTS: python3-dbus.

PRIVILEGE: ofono's SendMessage is denied to the normal user on Sailfish
C(org.ofono.Error.AccessDenied) but works as root, and Sailfish has no `sudo`.
send_sms() therefore QUEUES the message for the root priv service (priv_client ->
spool -> priv_service.py) when not already running as root; run as root (inside the
priv service, or a root spike) it sends directly via ofono.

LIBRARY USE:
    from sms_sender import send_sms, send_gps_sms, send_gps_sms_to_all
    res = send_sms("+4915...", "hello")          # generic
    send_gps_sms("+4915...", lat, lon, fix_datetime=...)        # GPS reply (SMS trigger)
    send_gps_sms_to_all(whitelist, lat, lon, fix_datetime=...)  # GPS reply (MQTT trigger)

CLI (testing -- sends a REAL SMS, may cost money):
    python3 sms_sender.py --discover
    python3 sms_sender.py --to +4915... --text "FMD test"
"""

import argparse
import collections
import logging
import os
import sys
import time

import dbus

log = logging.getLogger("fmd.sms_send")

OFONO = "org.ofono"
MM_IFACE = "org.ofono.MessageManager"
MSG_IFACE = "org.ofono.Message"

# `state` is the final ofono message state ("sent" / "failed"), "pending" if it
# did not settle within the wait, or None when not waited for / only queued.
SendResult = collections.namedtuple("SendResult", "success message_path error state")
SendResult.__new__.__defaults__ = (None,)


def _bus():
    return dbus.SystemBus()


def list_modems(bus=None):
    bus = bus or _bus()
    mgr = dbus.Interface(bus.get_object(OFONO, "/"), "org.ofono.Manager")
    return [str(path) for path, _props in mgr.GetModems()]


def send_sms(to, text, modem=None, bus=None, wait_state=0):
    """Send one SMS. Long text is automatically split into multipart by ofono.

    With `wait_state` > 0 (seconds, root path only) it additionally waits for
    ofono to report the final message state and logs it -- SendMessage only
    means "accepted into ofono's queue", not "sent".

    Returns SendResult(success, message_path, error, state). Never raises.
    """
    # ofono SendMessage is not permitted to the normal user, and Sailfish has no
    # sudo; when not already root (e.g. the user command daemon), queue the message
    # for the root priv service, which sends it. Fire-and-forget: "success" here
    # means "queued".
    if os.geteuid() != 0:
        import priv_client
        ok = priv_client.send_sms(to, text)
        return SendResult(ok, None, None if ok else "could not queue SMS")
    watcher = None
    if wait_state and bus is None:
        try:
            watcher = _StateWatcher()
            bus = watcher.bus
        except Exception as exc:
            log.warning("cannot watch SMS state (%s); sending without", exc)
            watcher = None
    bus = bus or _bus()
    try:
        if not modem:
            modems = list_modems(bus)
            if not modems:
                return SendResult(False, None, "no modem found")
            modem = modems[0]
        mm = dbus.Interface(bus.get_object(OFONO, modem), MM_IFACE)
        path = str(mm.SendMessage(to, text))
        log.info("SMS to %s accepted by ofono via %s (%s)", to, modem, path)
    except Exception as exc:
        log.error("SendMessage to %s failed: %s", to, exc)
        if watcher:
            watcher.close()
        return SendResult(False, None, str(exc))
    if watcher is None:
        return SendResult(True, path, None)
    try:
        state = watcher.wait(path, wait_state, modem)
    except Exception as exc:
        state = "unknown (watch failed: %s)" % exc
    watcher.close()
    if state == "sent":
        log.info("SMS to %s: ofono state sent (%s)", to, path)
        return SendResult(True, path, None, state)
    if state == "failed":
        log.error("SMS to %s: ofono state failed (%s)", to, path)
        return SendResult(False, path, "ofono state failed", state)
    log.warning("SMS to %s: ofono state %s (%s)", to, state, path)
    return SendResult(True, path, None, state)


class _StateWatcher(object):
    """Track the final ofono state of one outgoing SMS.

    Two independent sources, because either may fail on a given ofono build:
      * signals: org.ofono.Message.PropertyChanged(State) and
        MessageManager.MessageRemoved, subscribed BEFORE SendMessage is called
        (ofono may report "sent" before the call even returns to us);
      * polling: MessageManager.GetMessages() lists the messages ofono is still
        working on, with their State -- once ours is gone, ofono is done.

    dbus-python dispatches signals on the process-DEFAULT GLib context (a
    private, thread-default context is not honoured -- a field test received
    no signal at all that way), so this iterates the default context. Only use
    it in a process where nothing else runs that context, i.e. the
    single-threaded root priv service.
    """

    FINAL = ("sent", "failed")
    POLL_S = 1.0

    def __init__(self):
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
        self._ctx = GLib.MainContext.default()
        self.bus = dbus.SystemBus(mainloop=DBusGMainLoop(), private=True)
        self._events = {}   # message path -> list of (t, what)
        self._t0 = time.monotonic()
        self._matches = [
            self.bus.add_signal_receiver(
                self._on_prop, dbus_interface=MSG_IFACE,
                signal_name="PropertyChanged", bus_name=OFONO, path_keyword="path"),
            self.bus.add_signal_receiver(
                self._on_removed, dbus_interface=MM_IFACE,
                signal_name="MessageRemoved", bus_name=OFONO),
        ]

    def _add(self, path, what):
        events = self._events.setdefault(str(path), [])
        # Polling repeats the same state every second; keep only changes.
        if not events or events[-1][1] != what:
            events.append((time.monotonic() - self._t0, what))

    def _on_prop(self, name, value, path=None):
        if str(name) == "State":
            self._add(path, "signal:%s" % value)

    def _on_removed(self, path):
        self._add(path, "signal:removed")

    def _poll(self, mm, path):
        try:
            for msg_path, props in mm.GetMessages():
                if str(msg_path) == path:
                    self._add(path, "poll:%s" % props.get("State", "?"))
                    return
            self._add(path, "poll:gone")
        except Exception as exc:
            self._add(path, "poll:error %s" % exc.__class__.__name__)

    def _states(self, path):
        return [w.split(":", 1)[1] for _t, w in self._events.get(path, [])]

    def wait(self, path, timeout, modem):
        """Return the final state of `path`: 'sent' / 'failed' (reported by
        ofono), 'done (state unknown)' (ofono dropped the message without us
        seeing its last state) or 'pending' (still queued after `timeout`)."""
        mm = dbus.Interface(self.bus.get_object(OFONO, modem), MM_IFACE)
        deadline = time.monotonic() + timeout
        next_poll = 0.0
        while time.monotonic() < deadline:
            while self._ctx.pending():
                self._ctx.iteration(False)
            if time.monotonic() >= next_poll:
                self._poll(mm, path)
                next_poll = time.monotonic() + self.POLL_S
            states = self._states(path)
            if any(st in self.FINAL or st in ("removed", "gone") for st in states):
                # Let a "sent" signal that races the removal still arrive.
                end = time.monotonic() + 0.5
                while time.monotonic() < end:
                    if not self._ctx.iteration(False):
                        time.sleep(0.05)
                break
            time.sleep(0.1)
        events = self._events.get(path, [])
        log.info("ofono state history for %s: %s", path,
                 ", ".join("%s@%.1fs" % (w, t) for t, w in events) or "none")
        states = self._states(path)
        for st in reversed(states):
            if st in self.FINAL:
                return st
        if "removed" in states or "gone" in states:
            return "done (state unknown)"
        return "pending"

    def close(self):
        for m in self._matches:
            try:
                m.remove()
            except Exception:
                pass
        try:
            self.bus.close()
        except Exception:
            pass


def format_gps_sms(lat, lon, fix_datetime=None, db_timestamp_local=None):
    """Build the GPS reply SMS body in the agreed format.

    If `fix_datetime` is given, it was a live fix. Otherwise the last known
    position from SQLite is used and `db_timestamp_local` is shown with a
    "last FIX from DB." note.

        GPS-Coordinates:
        <lat> <lon>
        <OSM link>
        <fix datetime>            (live)
        <db timestamp> last FIX from DB.   (fallback)
    """
    osm = ("https://www.openstreetmap.org/?mlat={:.6f}&mlon={:.6f}"
           "#map=17/{:.6f}/{:.6f}").format(float(lat), float(lon),
                                           float(lat), float(lon))
    lines = ["GPS-Coordinates:",
             "{:.6f} {:.6f}".format(float(lat), float(lon)),
             osm]
    if fix_datetime:
        lines.append(str(fix_datetime))
    else:
        lines.append("{} last FIX from DB.".format(db_timestamp_local or "unknown"))
    return "\n".join(lines)


def send_gps_sms(to, lat, lon, fix_datetime=None, db_timestamp_local=None,
                 modem=None, bus=None):
    """Convenience: format + send the GPS reply SMS to one recipient."""
    body = format_gps_sms(lat, lon, fix_datetime, db_timestamp_local)
    return send_sms(to, body, modem=modem, bus=bus)


def send_gps_sms_to_all(recipients, lat, lon, fix_datetime=None,
                        db_timestamp_local=None, modem=None, bus=None):
    """Send the GPS reply to every recipient (MQTT-triggered GPS command).

    Returns a dict {recipient: SendResult}.
    """
    body = format_gps_sms(lat, lon, fix_datetime, db_timestamp_local)
    results = {}
    for rcpt in recipients:
        results[rcpt] = send_sms(rcpt, body, modem=modem, bus=bus)
    return results


def _discover(bus):
    try:
        modems = list_modems(bus)
    except Exception as exc:
        log.error("cannot query modems: %s", exc)
        return 2
    log.info("modems: %s", modems)
    for m in modems:
        try:
            xml = dbus.Interface(
                bus.get_object(OFONO, m),
                "org.freedesktop.DBus.Introspectable").Introspect()
            log.info("  %s -> MessageManager present: %s",
                     m, MM_IFACE in str(xml))
        except Exception as exc:
            log.warning("  %s introspect failed: %s", m, exc)
    return 0


def _main(argv=None):
    ap = argparse.ArgumentParser(description="FMD outgoing SMS.")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--to", default=None)
    ap.add_argument("--text", default="FMD test message")
    ap.add_argument("--modem", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bus = _bus()
    if args.discover:
        return _discover(bus)
    if not args.to:
        log.error("--to is required (or use --discover)")
        return 2
    res = send_sms(args.to, args.text, modem=args.modem, bus=bus)
    print("RESULT success={} path={} error={}".format(
        1 if res.success else 0, res.message_path, res.error))
    return 0 if res.success else 1


if __name__ == "__main__":
    sys.exit(_main())
