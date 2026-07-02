#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""priv_client.py -- queue a privileged action for the root priv service.

The user daemon has no way to run something as root on Sailfish (no sudo), so it
writes a small JSON request into the spool directory watched by the root
`harbour-find-my-device-priv.path` unit (see priv_service.py). Writes are atomic
(temp file + rename) so the watcher never sees a half-written request.

Fire-and-forget: returns True if the request was queued, not when it completes.
"""

import json
import logging
import os
import tempfile

# Created by the tmpfiles drop-in owned by the primary Sailfish user (uid 100000),
# which is the user this daemon runs as, so it may add files here.
SPOOL_DIR = "/run/harbour-find-my-device/spool"

log = logging.getLogger("fmd.priv_client")


def queue(request):
    """Atomically write one request dict to the spool. Returns True on success."""
    try:
        fd, tmp = tempfile.mkstemp(dir=SPOOL_DIR, prefix="req-", suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(request, fh)
        os.rename(tmp, tmp[:-len(".tmp")] + ".json")
        return True
    except Exception as exc:
        log.error("could not queue privileged request %r: %s",
                  request.get("cmd"), exc)
        return False


def send_sms(to, body):
    """Queue an outgoing SMS (sent as root by the priv service)."""
    ok = queue({"cmd": "sendsms", "to": to, "body": body or ""})
    if ok:
        log.info("SMS to %s queued for the privileged service", to)
    return ok


def reboot():
    """Queue a device reboot (performed as root by the priv service)."""
    ok = queue({"cmd": "reboot"})
    if ok:
        log.info("reboot queued for the privileged service")
    return ok