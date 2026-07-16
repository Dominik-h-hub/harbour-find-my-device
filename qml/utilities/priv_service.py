#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""priv_service.py -- root-side processor for privileged actions.

Sailfish OS has no `sudo`, so the (non-root) user daemon cannot escalate directly.
Instead it drops a small JSON request file into a spool directory; a systemd SYSTEM
service (this script, running as root) is started by a `.path` unit whenever the
spool is non-empty, and performs the privileged action. This is the whole
privilege boundary; it can ONLY reboot, send an SMS, or set the system
location (GPS) switch.

Spool request files (one JSON object per file):
    {"cmd": "reboot"}
    {"cmd": "sendsms", "to": "+49...", "body": "text (may contain newlines)"}
    {"cmd": "location", "enable": true}

Every file is deleted after processing (success or failure) so the `.path` unit
does not retrigger endlessly.

ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/priv_service.py
"""

import glob
import json
import logging
import os
import stat
import subprocess
import sys

# Where the user daemon queues requests (see priv_client.py); the tmpfiles drop-in
# creates the directory owned by the primary user (uid 100000).
SPOOL_DIR = "/run/harbour-find-my-device/spool"
UTILS_DIR = "/usr/share/harbour-find-my-device/qml/utilities"

log = logging.getLogger("fmd.priv")


def _do_reboot():
    log.warning("reboot requested")
    # SFOS 5.1 dropped /sbin/reboot (usrmerge); systemctl works on 5.0 and 5.1.
    subprocess.call(["/usr/bin/systemctl", "reboot"])


def _do_sendsms(req):
    to = req.get("to")
    body = req.get("body") or ""
    if not to:
        log.error("sendsms: missing recipient")
        return
    # sms_sender.send_sms runs the ofono call directly when euid == 0 (we are root).
    if UTILS_DIR not in sys.path:
        sys.path.insert(0, UTILS_DIR)
    import sms_sender
    res = sms_sender.send_sms(to, body)
    log.info("sendsms to %s -> success=%s error=%s", to, res.success, res.error)


def _do_location(req):
    enable = bool(req.get("enable"))
    # As root, location_control writes the conf files itself (temp + rename with
    # preserved root:privileged ownership), which is what the Settings GUI's
    # file watcher needs to pick the change up.
    if UTILS_DIR not in sys.path:
        sys.path.insert(0, UTILS_DIR)
    import location_control
    results = location_control.set_location_enabled(enable=enable)
    log.info("location -> enabled=%s (%s)", enable,
             ", ".join("%s: %s" % (r["path"], r["error"] or "ok") for r in results))


def _process(path):
    try:
        # Open first, validate afterwards via fstat() on the opened fd: checking
        # the path before opening would be racy (TOCTOU), as a same-uid writer
        # could swap the file between check and open.  O_NOFOLLOW rejects
        # symlinks at open time; O_NONBLOCK makes opening a FIFO return
        # immediately instead of blocking this oneshot root service (it has no
        # effect on regular-file reads).
        flags = (os.O_RDONLY
                 | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)
            # Reject anything but a plain file, and hard-linked files
            # (nlink > 1 means the inode is reachable through another name
            # outside the spool dir).
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                log.error("rejecting non-regular request file %s", path)
                return
            # Require the request to be owned by the same uid that owns the
            # spool directory (the primary user, uid 100000 on Sailfish OS).
            spool_uid = os.stat(SPOOL_DIR).st_uid
            if st.st_uid != spool_uid:
                log.error(
                    "rejecting request file %s not owned by spool uid %s (uid=%s)",
                    path, spool_uid, st.st_uid,
                )
                return
            fh = os.fdopen(fd, "r")
            fd = -1  # ownership passed to fh
            with fh:
                req = json.load(fh)
        finally:
            if fd >= 0:
                os.close(fd)
    except Exception as exc:
        log.error("bad request file %s: %s", path, exc)
        return
    cmd = (req.get("cmd") or "").lower()
    try:
        if cmd == "reboot":
            _do_reboot()
        elif cmd == "sendsms":
            _do_sendsms(req)
        elif cmd == "location":
            _do_location(req)
        else:
            log.warning("unknown privileged command: %r", cmd)
    except Exception:
        log.exception("privileged command %r failed", cmd)


def main():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FMD_LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not os.path.isdir(SPOOL_DIR):
        return 0
    for path in sorted(glob.glob(os.path.join(SPOOL_DIR, "*.json"))):
        try:
            _process(path)
        finally:
            # Always remove the request so the .path unit does not loop.
            try:
                os.remove(path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())