#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db.py -- SQLite access layer for harbour-find-my-device.

Owns the connection settings and the schema. Every connection enables WAL and
foreign keys.
"""

import logging
import sqlite3

from . import paths

log = logging.getLogger("fmd.db")

_BUSY_TIMEOUT_MS = 5000
_schema_ready = False

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS gpsdata (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,
    Device_Id       TEXT    NOT NULL,
    Timestamp_utc   TEXT    NOT NULL,
    Timestamp_local TEXT,
    Latitude        REAL,
    Longitude       REAL,
    Altitude        REAL,
    Speed           REAL,
    Accuracy        REAL,
    Battery_level   INTEGER
);

-- own device + any added remote devices. Secrets stored OBFUSCATED.
CREATE TABLE IF NOT EXISTS devices (
    Id                INTEGER PRIMARY KEY AUTOINCREMENT,
    Device_Id         TEXT    NOT NULL UNIQUE
                              CHECK (Device_Id NOT LIKE '% %' AND Device_Id <> ''),
    Device_Label      TEXT,
    Is_Own            INTEGER NOT NULL DEFAULT 0 CHECK (Is_Own IN (0, 1)),
    Pin               TEXT,
    Totp_Secret       TEXT,
    Last_Auth_Result  TEXT    CHECK (Last_Auth_Result IN
                              ('ok', 'auth_failed', 'disabled') OR Last_Auth_Result IS NULL),
    Last_Ack_Utc      TEXT,
    Created_Utc       TEXT    NOT NULL,
    Updated_Utc       TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_single_own
    ON devices (Is_Own) WHERE Is_Own = 1;

CREATE TRIGGER IF NOT EXISTS trg_devices_updated
AFTER UPDATE ON devices
FOR EACH ROW
BEGIN
    UPDATE devices SET Updated_Utc = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    WHERE Id = OLD.Id;
END;

CREATE TABLE IF NOT EXISTS backup_codes (
    Id           INTEGER PRIMARY KEY AUTOINCREMENT,
    Device_Id    TEXT    NOT NULL,
    Code_Hash    TEXT    NOT NULL UNIQUE,
    Used         INTEGER NOT NULL DEFAULT 0 CHECK (Used IN (0, 1)),
    Used_Utc     TEXT,
    Created_Utc  TEXT    NOT NULL,
    FOREIGN KEY (Device_Id) REFERENCES devices (Device_Id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backup_codes_lookup
    ON backup_codes (Device_Id, Used);

-- key/value settings; the daemons read the feature toggles from here too.
CREATE TABLE IF NOT EXISTS settings (
    Key   TEXT PRIMARY KEY,
    Value TEXT
);
"""


def connect(path=None):
    """Open a connection with the project-wide pragmas applied.

    Caller is responsible for closing it (or use the `connection()` context
    manager). Row access is by name via sqlite3.Row.
    """
    global _schema_ready
    path = path or paths.db_path()
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    # WAL keeps reads from blocking the daemon's writes; FK ON enables cascade.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = %d;" % _BUSY_TIMEOUT_MS)
    # First connection in this process: make sure the schema exists. Applied
    # directly on this connection (not via init_schema) to avoid recursing back
    # into connect(). Only the default DB path is auto-ensured.
    if not _schema_ready and path == paths.db_path():
        conn.executescript(_SCHEMA)
        conn.commit()
        _schema_ready = True
        log.info("schema ensured at %s (on first connect)", path)
    return conn


class connection(object):
    """Context manager: `with connection() as conn:` -- commits on success."""

    def __init__(self, path=None):
        self._path = path
        self._conn = None

    def __enter__(self):
        self._conn = connect(self._path)
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
        finally:
            self._conn.close()
        return False


def init_schema(path=None):
    """Create all tables/indexes/triggers if missing. Safe to call repeatedly."""
    with connection(path) as conn:
        conn.executescript(_SCHEMA)
    log.info("schema ensured at %s", path or paths.db_path())
