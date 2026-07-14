#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
location_control.py -- Headless control of Sailfish OS location services.

WHAT THE SPIKE CONFIRMED
  Writing enabled=true to /var/lib/location/location.conf succeeds as root.
  The positioning provider is geoclue-providers-hybris (D-Bus activated).

CLI:
    python3 location_control.py status
    python3 location_control.py enable
    python3 location_control.py disable
"""

import argparse
import errno
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time

log = logging.getLogger("fmd.location")

# The device exposed the flag in BOTH of these; which one the running geoclue
# reads is not guaranteed, so we write both. Add a per-user path here if a
# device turns out to use one.
DEFAULT_PATHS = (
    "/var/lib/location/location.conf",
    "/etc/location/location.conf",
)

# Keys inside the [location] section. Note the literal backslash in 'gps\enabled'
# (QSettings-style nested key) -- handled safely below.
KEY_ENABLED = "enabled"
KEY_GPS_ENABLED = "gps\\enabled"
KEY_AGREEMENT = "agreement_accepted"


def _set_key(text, key, value):
    """Replace 'key=...' in the [location] section, preserving file format.

    Uses a function replacement so backslash keys (gps\\enabled) don't trip the
    regex replacement-template parser. Inserts the key under [location] if it
    is not present.
    """
    repl = "{}={}".format(key, value)
    pat = re.compile(r"^" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
    if pat.search(text):
        return pat.sub(lambda m: repl, text, count=1)
    return re.sub(r"^\[location\]\s*$",
                  lambda m: "[location]\n" + repl,
                  text, count=1, flags=re.MULTILINE)


def _read_key(text, key):
    m = re.search(r"^" + re.escape(key) + r"\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    return m.group(1) if m else None


def _write_conf(path, text):
    #Write the conf via temp file + atomic rename, like the Settings GUI does.

    d = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(prefix=".location.conf.", dir=d)
    except OSError:
        # Directory not writable for us (e.g. /etc/location as plain user):
        # fall back to the in-place rewrite; the GUI watches the /var/lib copy.
        with open(path, "w") as fh:
            fh.write(text)
        return
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        try:
            st = os.stat(path)
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            if hasattr(os, "chown"):
                os.chown(tmp, st.st_uid, st.st_gid)  # only effective as root
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path):
    """Return {'path', 'exists', 'enabled', 'gps_enabled', 'agreement'} for a file."""
    state = {"path": path, "exists": os.path.exists(path),
             "enabled": None, "gps_enabled": None, "agreement": None}
    if not state["exists"]:
        return state
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return state
    state["enabled"] = _read_key(text, KEY_ENABLED)
    state["gps_enabled"] = _read_key(text, KEY_GPS_ENABLED)
    state["agreement"] = _read_key(text, KEY_AGREEMENT)
    return state


def is_enabled(paths=DEFAULT_PATHS):
    """True if any known config has enabled=true."""
    for p in paths:
        if read_state(p).get("enabled") == "true":
            return True
    return False


def wait_until_enabled(timeout=3.0, poll=0.3, paths=DEFAULT_PATHS):
    """Wait until is_enabled() turns true; returns the final state.

    After set_location_enabled() the authoritative write may be performed
    asynchronously by the root priv service, so the flag can lag by a moment.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_enabled(paths):
            return True
        time.sleep(poll)
    return is_enabled(paths)


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def set_location_enabled(enable=True, accept_agreement=True, enable_gps=True,
                         paths=DEFAULT_PATHS, backup=True):
    """Set the location master switch across all known config files.

    Returns a list of per-file result dicts. Logs each change (debugging aid the
    daemon relies on).
    """
    value = "true" if enable else "false"
    results = []
    for path in paths:
        res = {"path": path, "written": False, "error": None}
        if not os.path.exists(path):
            res["error"] = "missing"
            results.append(res)
            continue
        try:
            with open(path, "r") as fh:
                text = fh.read()
            if backup:
                bak = path + ".fmd.bak"
                if not os.path.exists(bak):
                    shutil.copy2(path, bak)
                    log.info("backup created: %s", bak)

            text = _set_key(text, KEY_ENABLED, value)
            if enable and enable_gps:
                text = _set_key(text, KEY_GPS_ENABLED, "true")
            if enable and accept_agreement:
                text = _set_key(text, KEY_AGREEMENT, "true")

            _write_conf(path, text)
            res["written"] = True
            log.info("location %s in %s (gps=%s, agreement=%s)",
                     "ENABLED" if enable else "DISABLED", path,
                     enable_gps if enable else "-",
                     accept_agreement if enable else "-")
        except OSError as exc:
            res["error"] = str(exc)
            if exc.errno == errno.EACCES:
                log.warning("cannot write %s (%s); skipping -- geoclue is D-Bus "
                            "activated, so this is non-fatal", path, exc)
            else:
                log.error("failed writing %s: %s", path, exc)
        results.append(res)

    # Stock Sailfish has location.conf and its directory owned root:privileged;
    # a plain app process can at best rewrite the file in place (once the GUI
    # has written it and left it user-owned), which the Settings GUI's file
    # watcher does NOT notice. Queue the change to the root priv service too:
    # it re-writes via rename with preserved ownership, which the GUI (and any
    # other watcher) picks up. The direct write above stays as best effort so
    # geoclue sees the change immediately where the file is writable.
    if not _is_root():
        try:
            import priv_client
            priv_client.set_location(enable)
        except Exception as exc:
            log.warning("could not queue location change to priv service: %s", exc)

    log.info("location set to enabled=%s; the change is applied when geoclue "
             "next starts (D-Bus activated, no service restart needed)", value)
    return results


def _main(argv=None):
    ap = argparse.ArgumentParser(description="FMD Sailfish location control.")
    ap.add_argument("action", choices=["status", "enable", "disable"])
    ap.add_argument("--no-agreement", action="store_true",
                    help="do NOT set agreement_accepted=true when enabling.")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.action == "status":
        for p in DEFAULT_PATHS:
            log.info("state: %s", read_state(p))
        log.info("effective is_enabled() = %s", is_enabled())
        return 0

    enable = args.action == "enable"
    set_location_enabled(enable=enable,
                         accept_agreement=not args.no_agreement,
                         backup=not args.no_backup)
    # Re-read to confirm what landed on disk.
    log.info("after write: is_enabled() = %s", is_enabled())
    if enable:
        log.info("NOTE: flag set. A real position fix must still be obtained by "
                 "the GPS daemon and may take seconds (cold start). Verify with "
                 "Settings > Location and an actual positioning test.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
