#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lock_control.py -- Remote device lock for harbour-find-my-device (Sailfish OS).

Distilled from the LOCK spike. Two mechanisms, both discovered on a
Fairphone 4 / Sailfish OS:

  1. SECURE device lock (primary) -- forces the security-code lock:
       dest   org.nemomobile.devicelock
       path   /devicelock
       iface  org.nemomobile.lipstick.devicelock
       call   setState(int32)        # 0 = unlocked, 1 = locked
       read   state() -> int, signal stateChanged(int)
     The device reports SecurityCodeSet = true, so this is meaningful.

  2. SCREEN lock (fallback) -- MCE tklock + display off (CONFIRMED working):
       dest   com.nokia.mce
       path   /com/nokia/mce/request
       iface  com.nokia.mce.request
       call   req_tklock_mode_change("locked"), req_display_state_off()

CONFIRMED: tklock locks the screen. The secure setState(1) call is built from
the introspected interface; verify it once with `lock_control.py secure` (you
can recover by entering your security code on the device).

"""

import argparse
import logging
import sys

import dbus

log = logging.getLogger("fmd.lock")

# Secure device lock
DL_DEST = "org.nemomobile.devicelock"
DL_PATH = "/devicelock"
DL_IFACE_LEGACY = "org.nemomobile.lipstick.devicelock"   # state()/setState()
DL_IFACE_NEW = "org.nemomobile.devicelock.DeviceLock"    # Unlock(), props
STATE_UNLOCKED = 0
STATE_LOCKED = 1

# MCE screen lock
MCE_DEST = "com.nokia.mce"
MCE_PATH = "/com/nokia/mce/request"
MCE_IFACE = "com.nokia.mce.request"


def _bus():
    return dbus.SystemBus()


def get_state():
    """Return the current secure-lock state as int (0=unlocked, 1=locked) or None."""
    try:
        obj = _bus().get_object(DL_DEST, DL_PATH)
        legacy = dbus.Interface(obj, DL_IFACE_LEGACY)
        return int(legacy.state())
    except Exception as exc:
        log.warning("could not read lock state: %s", exc)
        return None


def is_security_code_set():
    """True if a security code is configured (lock is meaningful)."""
    try:
        obj = _bus().get_object(DL_DEST, "/authenticator")
        props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
        return bool(props.Get("org.nemomobile.devicelock.SecurityCodeSettings",
                              "SecurityCodeSet"))
    except Exception as exc:
        log.warning("could not read SecurityCodeSet: %s", exc)
        return None


def lock_secure():
    """Force the secure (code) device lock via setState(1). Returns True on success."""
    try:
        obj = _bus().get_object(DL_DEST, DL_PATH)
        legacy = dbus.Interface(obj, DL_IFACE_LEGACY)
        legacy.setState(dbus.Int32(STATE_LOCKED))
        new_state = get_state()
        log.info("secure lock requested (state now=%s)", new_state)
        return new_state == STATE_LOCKED or new_state is None
    except Exception as exc:
        log.error("secure lock failed: %s", exc)
        return False


def lock_screen():
    """Lock the screen via MCE tklock and blank the display. Returns True on success."""
    try:
        mce = dbus.Interface(_bus().get_object(MCE_DEST, MCE_PATH), MCE_IFACE)
        mce.req_tklock_mode_change("locked")
        try:
            mce.req_display_state_off()
        except Exception as exc:
            log.info("display-off not sent: %s", exc)
        log.info("screen tklock = locked")
        return True
    except Exception as exc:
        log.error("screen lock failed: %s", exc)
        return False


def lock(prefer_secure=True):
    """Lock the device. Tries the secure lock first, falls back to screen lock.

    Returns one of: 'secure', 'screen', or '' (failure). The daemon maps this
    onto the cmd/ack reply.
    """
    if prefer_secure and lock_secure():
        return "secure"
    if lock_screen():
        return "screen"
    return ""


def unlock_for_testing():
    """RECOVERY helper for spikes/tests only. Prefer entering the code on device.

    Tries setState(0) and tklock unlocked; may or may not bypass the code
    depending on the platform.
    """
    ok = False
    try:
        obj = _bus().get_object(DL_DEST, DL_PATH)
        dbus.Interface(obj, DL_IFACE_LEGACY).setState(dbus.Int32(STATE_UNLOCKED))
        ok = True
    except Exception as exc:
        log.warning("setState(0) failed: %s", exc)
    try:
        mce = dbus.Interface(_bus().get_object(MCE_DEST, MCE_PATH), MCE_IFACE)
        mce.req_tklock_mode_change("unlocked")
        ok = True
    except Exception as exc:
        log.warning("tklock unlock failed: %s", exc)
    return ok


def _main(argv=None):
    ap = argparse.ArgumentParser(description="FMD device lock control.")
    ap.add_argument("action",
                    choices=["state", "secure", "screen", "lock", "unlock"],
                    help="state=read; secure=code lock; screen=tklock; "
                         "lock=secure then fallback; unlock=recovery (testing).")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.action == "state":
        log.info("secure lock state = %s (0=unlocked,1=locked)", get_state())
        log.info("security code set  = %s", is_security_code_set())
        return 0
    if args.action == "secure":
        return 0 if lock_secure() else 1
    if args.action == "screen":
        return 0 if lock_screen() else 1
    if args.action == "lock":
        result = lock()
        log.info("lock result = %r", result)
        return 0 if result else 1
    if args.action == "unlock":
        log.warning("recovery unlock (testing only); enter your code on device "
                    "if this does not release the lock")
        return 0 if unlock_for_testing() else 1
    return 2


if __name__ == "__main__":
    sys.exit(_main())
