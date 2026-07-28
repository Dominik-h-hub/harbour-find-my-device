#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paths.py -- Filesystem locations for harbour-find-my-device.

Single source of truth for where the app keeps its data. The same path must be
used by the UI process (PyOtherSide) and by both daemons (systemd user services),
because they all share the one SQLite DB.

The UI runs inside Sailjail, which maps the app's data directory to
~/.local/share/<OrganizationName>/<ApplicationName>/. With both names set to
"harbour-find-my-device" (see the .desktop [X-Sailjail] block) that is

    ~/.local/share/harbour-find-my-device/harbour-find-my-device/

Inside the sandbox this path is the app's real, persistent data dir; the
unsandboxed daemons see the very same path in the real home. The path is
composed explicitly instead of trusting $XDG_DATA_HOME: inside the sandbox
XDG_DATA_HOME already points here, outside (daemons) it does not.
"""

import logging
import os
import shutil

log = logging.getLogger("fmd.paths")

APP_NAME = "harbour-find-my-device"

# Logical names used across the codebase.
DB_FILENAME = "findmydevice.db"

_migration_done = False


def data_dir():
    """Return the app data directory, creating it if needed.

    Resolves to ~/.local/share/harbour-find-my-device/harbour-find-my-device/
    (Sailjail layout, see module docstring). The DB and any captured photos
    live here.
    """
    base = os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_NAME, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log.error("could not create data dir %s: %s", path, exc)
    _migrate_legacy_data(os.path.join(base, APP_NAME), path)
    return path


def _migrate_legacy_data(old_dir, new_dir):
    """One-time move of pre-Sailjail data into the new nested data dir.

    Before the Sailjail switch everything lived directly in
    ~/.local/share/harbour-find-my-device/ (the parent of the new dir). If a DB
    exists there but not yet in the new location, move it (plus WAL/SHM
    sidecars, photos/ and the ring state file) into the new dir. Runs at most
    once per process and only ever moves; nothing is deleted on failure.
    """
    global _migration_done
    if _migration_done:
        return
    _migration_done = True
    old_db = os.path.join(old_dir, DB_FILENAME)
    new_db = os.path.join(new_dir, DB_FILENAME)
    if not os.path.isfile(old_db) or os.path.exists(new_db):
        return
    log.info("migrating legacy data from %s to %s", old_dir, new_dir)
    entries = [DB_FILENAME, DB_FILENAME + "-wal", DB_FILENAME + "-shm",
               "photos", "ring_active"]
    for name in entries:
        src = os.path.join(old_dir, name)
        dst = os.path.join(new_dir, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            log.info("migrated %s", name)
        except OSError as exc:
            log.error("could not migrate %s: %s", name, exc)


def db_path():
    """Absolute path to the SQLite database file."""
    return os.path.join(data_dir(), DB_FILENAME)


def photos_dir():
    """Directory for camera captures before WebDAV upload."""
    path = os.path.join(data_dir(), "photos")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log.error("could not create photos dir %s: %s", path, exc)
    return path
