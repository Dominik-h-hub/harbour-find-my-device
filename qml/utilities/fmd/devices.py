#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""devices.py -- Device records (own device + remote devices).

The own device is created on first start with a generated 10-char id (the id is
also the MQTT topic name). Remote devices are added by the user with their id,
label and PIN. All PINs / TOTP secrets are obfuscated at rest (see obfuscation.py).
"""

import logging
import re
import secrets
import string
import time

from . import db
from .obfuscation import deobfuscate, obfuscate

log = logging.getLogger("fmd.devices")

ID_LENGTH = 10
_ID_ALPHABET = string.ascii_letters + string.digits          # A-Za-z0-9
_ID_RE = re.compile(r"^[A-Za-z0-9]{%d}$" % ID_LENGTH)


def iso_utc(t=None):
    t = t if t is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def generate_device_id():
    """Generate a 10-char id from upper/lower letters and digits (no spaces)."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(ID_LENGTH))


def is_valid_id(device_id):
    """True if id is exactly 10 chars of letters/digits."""
    return bool(device_id and _ID_RE.match(device_id))


# --- own device ------------------------------------------------------------
def get_own(conn=None):
    """Return the own-device row as a dict, or None if not created yet."""
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM devices WHERE Is_Own = 1 LIMIT 1").fetchone()
    finally:
        if own:
            conn.close()
    return _row_to_dict(row) if row else None


def ensure_own_device(conn=None):
    """Create the own device with a fresh id if it does not exist yet.

    Returns the own-device dict. Called on first app start. The id is unique
    across the table.
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        existing = conn.execute(
            "SELECT * FROM devices WHERE Is_Own = 1 LIMIT 1").fetchone()
        if existing:
            return _row_to_dict(existing)

        for _ in range(10):
            device_id = generate_device_id()
            clash = conn.execute(
                "SELECT 1 FROM devices WHERE Device_Id = ?", (device_id,)).fetchone()
            if not clash:
                break
        now = iso_utc()
        conn.execute(
            "INSERT INTO devices (Device_Id, Device_Label, Is_Own, Created_Utc) "
            "VALUES (?, ?, 1, ?)", (device_id, "", now))
        if own:
            conn.commit()
        log.info("own device created with generated id %s", device_id)
        return get_own(conn=conn)
    finally:
        if own:
            conn.close()


def own_device_id(conn=None):
    """Convenience: the own device's id string (creating it if needed)."""
    dev = ensure_own_device(conn=conn)
    return dev["device_id"] if dev else None


# --- remote devices --------------------------------------------------------
def add_remote(device_id, label, pin, conn=None):
    """Insert a remote device. Returns (ok, error_message)."""
    if not is_valid_id(device_id):
        return False, "Device-Id must be 10 letters/digits, no spaces"
    own = conn is None
    conn = conn or db.connect()
    try:
        clash = conn.execute(
            "SELECT 1 FROM devices WHERE Device_Id = ?", (device_id,)).fetchone()
        if clash:
            return False, "A device with this id already exists"
        now = iso_utc()
        conn.execute(
            "INSERT INTO devices (Device_Id, Device_Label, Is_Own, Pin, Created_Utc) "
            "VALUES (?, ?, 0, ?, ?)",
            (device_id, label or "", obfuscate(pin) if pin else None, now))
        if own:
            conn.commit()
        log.info("remote device added: %s (label=%r)", device_id, label)
        return True, None
    finally:
        if own:
            conn.close()


def update_device(device_id, label=None, pin=None, conn=None):
    """Update a remote device's label and/or PIN. Own device label uses this too
    (pass pin=None for own device). Returns (ok, error_message).
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM devices WHERE Device_Id = ?", (device_id,)).fetchone()
        if not row:
            return False, "Device not found"
        sets, params = [], []
        if label is not None:
            sets.append("Device_Label = ?")
            params.append(label)
        if pin is not None:
            sets.append("Pin = ?")
            params.append(obfuscate(pin) if pin else None)
        if not sets:
            return True, None
        params.append(device_id)
        conn.execute(
            "UPDATE devices SET %s WHERE Device_Id = ?" % ", ".join(sets), params)
        if own:
            conn.commit()
        log.info("device updated: %s (fields=%s)", device_id,
                 [s.split(" =")[0] for s in sets])
        return True, None
    finally:
        if own:
            conn.close()


def rename_device(old_id, new_id, conn=None):
    """Change a remote device's Device_Id (e.g. correcting a wrong id) and move its
    gpsdata rows along with it. The own device id is never renamed (it would orphan
    its backup_codes, which reference Device_Id without ON UPDATE CASCADE).
    Returns (ok, error_message).
    """
    if not is_valid_id(new_id):
        return False, "Device-Id must be 10 letters/digits, no spaces"
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT Is_Own FROM devices WHERE Device_Id = ?", (old_id,)).fetchone()
        if not row:
            return False, "Device not found"
        if row["Is_Own"] == 1:
            return False, "The own device id cannot be changed"
        clash = conn.execute(
            "SELECT 1 FROM devices WHERE Device_Id = ?", (new_id,)).fetchone()
        if clash:
            return False, "A device with this id already exists"
        conn.execute(
            "UPDATE devices SET Device_Id = ? WHERE Device_Id = ?", (new_id, old_id))
        conn.execute(
            "UPDATE gpsdata SET Device_Id = ? WHERE Device_Id = ?", (new_id, old_id))
        if own:
            conn.commit()
        log.info("device id changed: %s -> %s", old_id, new_id)
        return True, None
    finally:
        if own:
            conn.close()


def remove_device(device_id, conn=None):
    """Unpair a device: delete its devices row and its gpsdata rows.

    The own device cannot be removed (configured via Settings). backup_codes
    cascade automatically (FK ON DELETE CASCADE + foreign_keys=ON).
    Returns (ok, error_message).
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT Is_Own FROM devices WHERE Device_Id = ?", (device_id,)).fetchone()
        if not row:
            return False, "Device not found"
        if row["Is_Own"] == 1:
            return False, "The own device cannot be removed"
        conn.execute("DELETE FROM gpsdata WHERE Device_Id = ?", (device_id,))
        conn.execute("DELETE FROM devices WHERE Device_Id = ?", (device_id,))
        if own:
            conn.commit()
        log.info("device unpaired and data removed: %s", device_id)
        return True, None
    finally:
        if own:
            conn.close()


# --- queries ---------------------------------------------------------------
def list_devices(conn=None):
    """Return all devices as dicts (own first), PIN/secret de-obfuscated."""
    own = conn is None
    conn = conn or db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM devices ORDER BY Is_Own DESC, Device_Label, Device_Id"
        ).fetchall()
    finally:
        if own:
            conn.close()
    return [_row_to_dict(r) for r in rows]


def get_device(device_id, conn=None):
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM devices WHERE Device_Id = ?", (device_id,)).fetchone()
    finally:
        if own:
            conn.close()
    return _row_to_dict(row) if row else None


def get_pin(device_id, conn=None):
    """Return the de-obfuscated PIN for a device, or None."""
    dev = get_device(device_id, conn=conn)
    return dev.get("pin") if dev else None


def set_auth_result(device_id, result, conn=None):
    """Record the last cmd/ack result for a device (drives button greying)."""
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.execute(
            "UPDATE devices SET Last_Auth_Result = ?, Last_Ack_Utc = ? "
            "WHERE Device_Id = ?", (result, iso_utc(), device_id))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    log.info("auth result for %s set to %s", device_id, result)


def display_label(device):
    """Label to show; fall back to the id when no label is set."""
    label = (device.get("device_label") or "").strip()
    return label if label else device.get("device_id")


# --- helpers ---------------------------------------------------------------
def _row_to_dict(row):
    """sqlite3.Row -> plain dict with lowercased keys and de-obfuscated secrets."""
    d = {
        "id": row["Id"],
        "device_id": row["Device_Id"],
        "device_label": row["Device_Label"],
        "is_own": int(row["Is_Own"]),
        "pin": deobfuscate(row["Pin"]) if row["Pin"] else None,
        "totp_secret": deobfuscate(row["Totp_Secret"]) if row["Totp_Secret"] else None,
        "last_auth_result": row["Last_Auth_Result"],
        "last_ack_utc": row["Last_Ack_Utc"],
        "created_utc": row["Created_Utc"],
        "updated_utc": row["Updated_Utc"],
    }
    return d
