#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""runtime.py -- Cross-process runtime state shared through the SQLite DB.

The sandboxed UI must not talk to systemd or the priv spool, so all
GUI <-> daemon coordination happens through the `runtime` key/value table:

  * settings generation -- the UI bumps a counter after every settings save;
    the daemons poll it and reload/reconnect when it changed.
  * heartbeats -- each daemon periodically writes a timestamp, its state
    ("active" or "idle"), its installed package version and the settings
    generation that state was built from. The UI derives the daemon status
    shown in Settings purely from these records.
  * privileged requests -- the UI queues a "please enable system location"
    wish here; the (unsandboxed) cmd daemon picks it up and forwards it to
    the root priv service. The UI itself never touches /run/... .

Kept separate from the user-facing `settings` table so transient runtime
records never show up in get_all_public()/backups.
"""

import logging
import time

from . import db

log = logging.getLogger("fmd.runtime")

# A heartbeat younger than this counts as "daemon is running".
HEARTBEAT_FRESH_S = 90
# How often the daemons rewrite their heartbeat.
HEARTBEAT_INTERVAL_S = 30

_GENERATION_KEY = "settings_generation"
_LOCATION_REQUEST_KEY = "location_enable_request"


# --- low-level access ------------------------------------------------------
def get(key, default=None):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT Value FROM runtime WHERE Key = ?", (key,)).fetchone()
    return row["Value"] if row is not None else default


def set(key, value):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO runtime (Key, Value) VALUES (?, ?) "
            "ON CONFLICT(Key) DO UPDATE SET Value = excluded.Value",
            (key, "" if value is None else str(value)))


# --- settings generation (UI -> daemons reload signal) ---------------------
def generation():
    """Current settings generation counter (0 if never bumped)."""
    try:
        return int(get(_GENERATION_KEY, "0"))
    except (TypeError, ValueError):
        return 0


def bump_generation():
    """Signal the daemons that settings changed (called after every save)."""
    gen = generation() + 1
    set(_GENERATION_KEY, gen)
    log.info("settings generation -> %d", gen)
    return gen


# --- daemon heartbeats (daemons -> UI status) ------------------------------
def write_heartbeat(daemon, state, version="", generation=None):
    """Record one daemon heartbeat. `daemon` is "gps" or "cmd";
    `state` is "active" or "idle".

    `generation` is the settings generation the reported state was built from.
    Echoing it back lets the UI tell "the daemon has applied my save" from
    "the daemon has not woken up yet" instead of showing a stale state as if
    it were current. Pass None when that is unknown (error paths): the last
    recorded value is then left untouched rather than falsely confirmed.
    """
    now = int(time.time())
    rows = [("daemon_%s_heartbeat" % daemon, now),
            ("daemon_%s_state" % daemon, state),
            ("daemon_%s_version" % daemon, version or "")]
    if generation is not None:
        rows.append(("daemon_%s_applied_generation" % daemon, int(generation)))
    with db.connection() as conn:
        for key, value in rows:
            conn.execute(
                "INSERT INTO runtime (Key, Value) VALUES (?, ?) "
                "ON CONFLICT(Key) DO UPDATE SET Value = excluded.Value",
                (key, str(value)))


def _heartbeat_keys(daemon):
    return ("daemon_%s_heartbeat" % daemon, "daemon_%s_state" % daemon,
            "daemon_%s_version" % daemon,
            "daemon_%s_applied_generation" % daemon)


def _parse_heartbeat(data, daemon):
    """Build a heartbeat dict from an already-read {Key: Value} mapping."""
    beat_key, state_key, version_key, applied_key = _heartbeat_keys(daemon)
    age = None
    try:
        age = max(0, int(time.time()) - int(data.get(beat_key, "")))
    except (TypeError, ValueError):
        pass
    try:
        applied = int(data.get(applied_key, ""))
    except (TypeError, ValueError):
        applied = None
    return {"age_s": age,
            "state": data.get(state_key, ""),
            "version": data.get(version_key, ""),
            "applied_generation": applied}


def _status_from(hb, current_generation):
    """Map a heartbeat to the UI status string.

    'running'      -- fresh heartbeat, state active
    'deactivated'  -- fresh heartbeat, state idle (features off, daemon naps)
    'applying'     -- fresh heartbeat, but from before the last settings save;
                      the reported state is outdated, the daemon is still
                      napping and will pick the change up shortly
    'not_installed_or_stopped' -- heartbeat missing or stale

    The staleness check comes first: a dead daemon must read as stopped, not
    as forever "applying".
    """
    if hb["age_s"] is None or hb["age_s"] > HEARTBEAT_FRESH_S:
        return "not_installed_or_stopped"
    # None = daemon package too old to report it; then trust the state as-is.
    if hb["applied_generation"] is not None \
            and hb["applied_generation"] != current_generation:
        return "applying"
    return "running" if hb["state"] == "active" else "deactivated"


def read_heartbeat(daemon):
    """Return {'age_s': int|None, 'state': str, 'version': str,
    'applied_generation': int|None} for a daemon. applied_generation is None
    when the daemon never reported one (e.g. an older daemon package)."""
    keys = _heartbeat_keys(daemon)
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT Key, Value FROM runtime WHERE Key IN (?, ?, ?, ?)",
            keys).fetchall()
    return _parse_heartbeat({r["Key"]: r["Value"] for r in rows}, daemon)


def daemon_status(daemon):
    """UI status string for a single daemon (see _status_from)."""
    return _status_from(read_heartbeat(daemon), generation())


def daemon_snapshot(daemons=("gps", "cmd")):
    """{daemon: {'status': str, ...heartbeat fields}} from one DB connection.

    The Settings page polls this every couple of seconds; going through
    daemon_status()/read_heartbeat() per daemon would open (and schema-check)
    a connection five times per poll instead of once.
    """
    keys = [_GENERATION_KEY]
    for daemon in daemons:
        keys.extend(_heartbeat_keys(daemon))
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT Key, Value FROM runtime WHERE Key IN (%s)"
            % ",".join(["?"] * len(keys)), keys).fetchall()
    data = {r["Key"]: r["Value"] for r in rows}
    try:
        gen = int(data.get(_GENERATION_KEY, "0"))
    except (TypeError, ValueError):
        gen = 0
    out = {}
    for daemon in daemons:
        hb = _parse_heartbeat(data, daemon)
        hb["status"] = _status_from(hb, gen)
        out[daemon] = hb
    return out


# --- privileged requests (UI wish -> cmd daemon -> priv service) -----------
def request_location_enable():
    """UI-side: ask the cmd daemon to enable system location services."""
    set(_LOCATION_REQUEST_KEY, int(time.time()))
    log.info("location enable request queued for the cmd daemon")


def consume_location_enable_request(max_age_s=300):
    """Daemon-side: return True (and clear the flag) if a fresh request is
    pending. Stale requests are dropped silently."""
    raw = get(_LOCATION_REQUEST_KEY, "")
    if not raw:
        return False
    set(_LOCATION_REQUEST_KEY, "")
    try:
        return (int(time.time()) - int(raw)) <= max_age_s
    except (TypeError, ValueError):
        return False


# --- installed package versions --------------------------------------------
def read_version_file(path):
    """Return the stripped content of a VERSION file, or "" if unreadable."""
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""
