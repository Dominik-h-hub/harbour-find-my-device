#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""priv_service.py -- root-side processor for privileged actions.

Sailfish OS has no `sudo`, so the (non-root) user daemon cannot escalate directly.
Instead it drops a small JSON request file into a spool directory; a systemd SYSTEM
service (this script, running as root) is started by a `.path` unit whenever the
spool is non-empty, and performs the privileged action. This is the whole
privilege boundary; it can ONLY reboot or send an SMS.

Spool request files (one JSON object per file):
    {"cmd": "reboot"}
    {"cmd": "sendsms", "to": "+49...", "body": "text (may contain newlines)"}

Every file is deleted after processing (success or failure) so the `.path` unit
does not retrigger endlessly.

ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/priv_service.py
"""

import glob
import json
import logging
import os
import subprocess
import sys

# Where the user daemon queues requests (see priv_client.py); the tmpfiles drop-in
# creates the directory owned by the primary user (uid 100000).
SPOOL_DIR = "/run/harbour-find-my-device/spool"
UTILS_DIR = "/usr/share/harbour-find-my-device/qml/utilities"

log = logging.getLogger("fmd.priv")


def _do_reboot():
    log.warning("reboot requested")
    subprocess.call(["/sbin/reboot"])


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


def _process(path):
    try:
        with open(path) as fh:
            req = json.load(fh)
    except Exception as exc:
        log.error("bad request file %s: %s", path, exc)
        return
    cmd = (req.get("cmd") or "").lower()
    try:
        if cmd == "reboot":
            _do_reboot()
        elif cmd == "sendsms":
            _do_sendsms(req)
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