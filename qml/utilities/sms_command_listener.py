#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sms_command_listener.py -- Incoming-SMS remote control for harbour-find-my-device.

Distilled from the SMS spike. Listens for incoming SMS via the ofono D-Bus
signal org.ofono.MessageManager -> IncomingMessage (and ImmediateMessage),
applies the two-factor check (sender whitelist + one-time code) and hands valid
commands to a callback. Auth and execution are pluggable so this module stays
free of DB / secret handling.

CONFIRMED ON DEVICE
  Fairphone 4 / Sailfish OS, run as user: IncomingMessage delivers 'Sender' and
  the text on modem /ril_0 with no Messages app open.

REQUIREMENTS (RPM): python3-dbus, python3-gobject
  Run as user in the 'sailfish-radio' group.

COMMAND FORMAT (SMS body):
    KEYWORD [front|back] CODE
  e.g. "CAMERA front 123456", "LOCK 123456", "RING 123456", "DELETE 123456"
  CODE is a TOTP one-time code or a one-time backup code (see SMS_NOTES.md).

INTEGRATION
  The command daemon should run a GLib main loop (D-Bus needs one). Create the
  listener, call start(), and run the loop. MQTT (paho) can run in its own
  network thread alongside.

    listener = SmsCommandListener(
        allowed_senders=["+491523123456"],
        authorize=my_authorize,        # (cmd, arg, code, sender) -> bool
        on_command=my_execute)         # (cmd, arg, sender) -> None
    listener.start()
    GLib.MainLoop().run()
"""

import base64
import hashlib
import hmac
import logging
import struct
import time

import dbus
import dbus.mainloop.glib

log = logging.getLogger("fmd.sms")

KEYWORDS = ("CAMERA", "LOCK", "DELETE", "RING", "GPS")
CAMERA_ARGS = ("front", "back")
OFONO_MM_IFACE = "org.ofono.MessageManager"


# --- helpers ---------------------------------------------------------------
def normalize_number(num):
    """Reduce a phone number to digits only (drop +, spaces, dashes)."""
    return "".join(ch for ch in str(num) if ch.isdigit())


def sender_allowed(sender, allowed_senders, match_digits=9):
    """Whitelist check. Matches on the last `match_digits` digits so national
    and international formats of the same number compare equal. Set
    match_digits=0 to require an exact normalized match instead.
    """
    s = normalize_number(sender)
    for entry in allowed_senders:
        e = normalize_number(entry)
        if match_digits and len(s) >= match_digits and len(e) >= match_digits:
            if s[-match_digits:] == e[-match_digits:]:
                return True
        elif s == e:
            return True
    return False


def parse_command(text):
    """Parse 'KEYWORD [front|back] CODE'. Returns (cmd, arg, code) or None.

    For CAMERA, arg defaults to 'back' if not given. arg is None for others.
    """
    parts = (text or "").strip().split()
    if not parts:
        return None
    cmd = parts[0].upper()
    if cmd not in KEYWORDS:
        return None
    rest = parts[1:]
    arg = None
    if cmd == "CAMERA":
        if rest and rest[0].lower() in CAMERA_ARGS:
            arg = rest[0].lower()
            rest = rest[1:]
        else:
            arg = "back"
    code = rest[0] if rest else None
    if not code:
        return None
    return (cmd, arg, code)


def verify_totp(secret_b32, code, step=30, digits=6, window=1):
    """RFC 6238 TOTP check using only the standard library.

    Accepts the current time step +/- `window` to tolerate small clock drift.
    The secret is the device's own Base32 TOTP secret (stored obfuscated).
    """
    try:
        key = base64.b32decode(str(secret_b32).strip().upper(), casefold=True)
    except Exception:
        return False
    code = str(code).strip()
    now = time.time()
    for w in range(-window, window + 1):
        counter = int(now // step) + w
        mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = mac[-1] & 0x0F
        val = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
        if str(val).zfill(digits) == code:
            return True
    return False


# --- listener --------------------------------------------------------------
class SmsCommandListener:
    """Subscribes to ofono incoming-SMS signals and dispatches valid commands.

    allowed_senders : iterable of phone numbers (any format).
    authorize       : callable(cmd, arg, code, sender) -> bool. Should verify
                      the one-time code (TOTP / backup) AND that the requested
                      feature is enabled in settings. Whitelist is checked here
                      before authorize is called.
    on_command      : callable(cmd, arg, sender) -> None. Executes the action.
    match_digits    : whitelist suffix length (see sender_allowed()).
    """

    def __init__(self, allowed_senders, authorize, on_command,
                 match_digits=9):
        self._allowed = list(allowed_senders or [])
        self._authorize = authorize
        self._on_command = on_command
        self._match_digits = match_digits
        self._bus = None
        self._receivers = []

    def start(self):
        """Register D-Bus signal receivers. Requires a running GLib main loop."""
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SystemBus()
        for signal in ("IncomingMessage", "ImmediateMessage"):
            r = self._bus.add_signal_receiver(
                self._make_handler(signal),
                dbus_interface=OFONO_MM_IFACE,
                signal_name=signal)
            self._receivers.append(r)
        log.info("SMS listener started (whitelist entries: %d)", len(self._allowed))

    def run(self):
        """Convenience: start() plus a blocking GLib main loop."""
        from gi.repository import GLib
        self.start()
        GLib.MainLoop().run()

    def stop(self):
        for r in self._receivers:
            try:
                r.remove()
            except Exception:
                pass
        self._receivers = []

    def _make_handler(self, signal_name):
        def handler(message, info):
            try:
                self._handle(signal_name, message, info)
            except Exception:
                log.exception("error handling %s", signal_name)
        return handler

    def _handle(self, signal_name, message, info):
        sender = "unknown"
        try:
            sender = str(info.get("Sender", "unknown"))
        except Exception:
            pass
        text = str(message)
        log.info("incoming SMS (%s) from %s", signal_name, sender)

        if not sender_allowed(sender, self._allowed, self._match_digits):
            log.warning("sender %s not in whitelist -> ignored", sender)
            return

        parsed = parse_command(text)
        if parsed is None:
            log.info("no valid command in SMS body -> ignored")
            return
        cmd, arg, code = parsed

        if not self._authorize(cmd, arg, code, sender):
            log.warning("authorization FAILED for %s from %s", cmd, sender)
            return

        log.info("authorized command %s arg=%s from %s -> executing", cmd, arg, sender)
        self._on_command(cmd, arg, sender)


# --- standalone safe monitor (does NOT execute anything) -------------------
def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="SMS command monitor (parses + logs, never executes).")
    ap.add_argument("--allow", action="append", default=[],
                    help="whitelisted sender (repeatable). Empty = allow all (test only).")
    ap.add_argument("--totp-secret", default=None,
                    help="if given, codes are TOTP-verified; else any code 'passes'.")
    ap.add_argument("--match-digits", type=int, default=9)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    allow = args.allow or ["*"]  # '*' sentinel = allow all in test mode

    def authorize(cmd, arg, code, sender):
        if args.totp_secret:
            ok = verify_totp(args.totp_secret, code)
            log.info("TOTP check for code=%s -> %s", code, ok)
            return ok
        log.info("no --totp-secret given: treating code as valid (TEST ONLY)")
        return True

    def on_command(cmd, arg, sender):
        log.info("WOULD EXECUTE: cmd=%s arg=%s from=%s (monitor mode, no action)",
                 cmd, arg, sender)

    senders = [] if allow == ["*"] else allow
    if allow == ["*"]:
        log.warning("no --allow given: accepting ALL senders (test mode only!)")

        def sender_ok(*_a, **_k):
            return True
        global sender_allowed  # noqa: PLW0603 - intentional test override
        sender_allowed = sender_ok

    listener = SmsCommandListener(senders, authorize, on_command,
                                  match_digits=args.match_digits)
    log.info("monitor running; send an SMS to the device. Ctrl+C to stop.")
    try:
        listener.run()
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
