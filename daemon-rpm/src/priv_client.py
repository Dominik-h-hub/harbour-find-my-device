#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""priv_client.py -- queue a privileged action for the root priv service.

The user daemon has no way to run something as root on Sailfish (no sudo), so it
writes a small JSON request into the spool directory watched by the root
`harbour-find-my-device-priv.path` unit (see priv_service.py). Writes are atomic
(temp file + rename) so the watcher never sees a half-written request.

Fire-and-forget: returns True if the request was queued, not when it completes.
For diagnostics every request carries an id (the spool file name, also logged by
the priv service) and a background check logs whether the priv service picked it
up -- if not, together with the state of the root path unit.
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import time

# Created by the tmpfiles drop-in owned by the primary Sailfish user (uid 100000),
# which is the user this daemon runs as, so it may add files here.
SPOOL_DIR = "/run/harbour-find-my-device/spool"

PRIV_PATH_UNIT = "harbour-find-my-device-priv.path"
PRIV_SERVICE_UNIT = "harbour-find-my-device-priv.service"

# How long the priv service may take to remove a request from the spool before
# the check below reports it as not consumed.
CONSUME_TIMEOUT_S = 15.0
CONSUME_POLL_S = 0.25

log = logging.getLogger("fmd.priv_client")


def _unit_state(unit):
    """'ActiveState=.. SubState=.. Result=..' of a system unit (read-only, works
    unprivileged), or a short error string."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveState,SubState,Result", unit],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=5).stdout
        return " ".join(out.split()) or "no output"
    except Exception as exc:
        return "unavailable (%s)" % exc


def _spool_listing():
    """Spool entries with their age, e.g. 'req-x.json(3.1s) req-y.tmp(120.0s)'."""
    try:
        now = time.time()
        entries = []
        for name in sorted(os.listdir(SPOOL_DIR)):
            try:
                age = now - os.lstat(os.path.join(SPOOL_DIR, name)).st_mtime
                entries.append("%s(%.1fs)" % (name, age))
            except OSError:
                entries.append("%s(?)" % name)
        return " ".join(entries) or "empty"
    except Exception as exc:
        return "unreadable (%s)" % exc


def log_status(level=logging.INFO):
    """Log the state of the root path watcher + service and the spool content."""
    log.log(level, "priv path unit: %s | priv service: %s | spool: %s",
            _unit_state(PRIV_PATH_UNIT), _unit_state(PRIV_SERVICE_UNIT),
            _spool_listing())


def _watch_consumed(req_id, path, queued_at):
    deadline = queued_at + CONSUME_TIMEOUT_S
    while time.monotonic() < deadline:
        if not os.path.exists(path):
            log.info("priv request %s consumed after %.1fs",
                     req_id, time.monotonic() - queued_at)
            return
        time.sleep(CONSUME_POLL_S)
    log.warning("priv request %s NOT consumed after %.0fs -- priv service not "
                "running?", req_id, CONSUME_TIMEOUT_S)
    log_status(logging.WARNING)


def queue(request):
    """Atomically write one request dict to the spool. Returns True on success."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=SPOOL_DIR, prefix="req-", suffix=".tmp")
        final = tmp[:-len(".tmp")] + ".json"
        req_id = os.path.basename(final)[:-len(".json")]
        request = dict(request, id=req_id)
        with os.fdopen(fd, "w") as fh:
            json.dump(request, fh)
        os.rename(tmp, final)
        tmp = None
    except Exception as exc:
        log.error("could not queue privileged request %r: %s",
                  request.get("cmd"), exc)
        if tmp is not None:
            # A leftover file keeps the spool non-empty and makes the path
            # unit retrigger the priv service over and over.
            try:
                os.remove(tmp)
            except OSError as rm_exc:
                log.error("could not remove stale spool file %s: %s", tmp, rm_exc)
        return False
    log.info("priv request %s queued (cmd=%s)", req_id, request.get("cmd"))
    threading.Thread(target=_watch_consumed,
                     args=(req_id, final, time.monotonic()),
                     daemon=True, name="fmd-priv-watch").start()
    return True


def send_sms(to, body):
    """Queue an outgoing SMS (sent as root by the priv service)."""
    ok = queue({"cmd": "sendsms", "to": to, "body": body or ""})
    if ok:
        log.info("SMS to %s queued for the privileged service (%d chars)",
                 to, len(body or ""))
    return ok


def reboot():
    """Queue a device reboot (performed as root by the priv service)."""
    ok = queue({"cmd": "reboot"})
    if ok:
        log.info("reboot queued for the privileged service")
    return ok


def set_location(enable):
    """Queue a system location on/off change (written as root by the priv service).

    location.conf and its directory are root:privileged on stock Sailfish OS, so
    only the root service can update them the way the Settings GUI expects
    (rename with preserved ownership -- see location_control._write_conf).
    """
    ok = queue({"cmd": "location", "enable": bool(enable)})
    if ok:
        log.info("location %s queued for the privileged service",
                 "enable" if enable else "disable")
    return ok
