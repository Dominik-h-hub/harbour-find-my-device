#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paths.py -- Filesystem locations for harbour-find-my-device.

Single source of truth for where the app keeps its data. The same path must be
used by the UI process (PyOtherSide) and by both daemons (systemd user services),
because they all share the one SQLite DB. Everything lives under the standard
Sailfish per-user data directory, so no root-owned files appear.
"""

import logging
import os

log = logging.getLogger("fmd.paths")

APP_NAME = "harbour-find-my-device"

# Logical names used across the codebase.
DB_FILENAME = "findmydevice.db"


def data_dir():
    """Return the app data directory, creating it if needed.

    Resolves to $XDG_DATA_HOME/<app> or ~/.local/share/<app> -- the standard
    Sailfish OS application data path. The DB and any captured photos live here.
    """
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log.error("could not create data dir %s: %s", path, exc)
    return path


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
