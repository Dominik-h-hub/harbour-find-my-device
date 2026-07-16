#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gpsstore.py -- Read/write the gpsdata table.

Only the LATEST row per device is kept: after inserting a new fix
we delete older rows for that device. The structure is left open for longer
retention later (just skip the prune step).
"""

import logging
from . import db

log = logging.getLogger("fmd.gpsstore")


def store_fix(device_id, timestamp_utc, timestamp_local, lat, lon,
              alt=None, speed=None, accuracy=None, battery=None,
              keep_latest_only=True, conn=None):
    """Insert one fix and (default) prune older rows for that device.

    Logs the write -- the map and the daemons rely on this for debugging.
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO gpsdata (Device_Id, Timestamp_utc, Timestamp_local, "
            "Latitude, Longitude, Altitude, Speed, Accuracy, Battery_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, timestamp_utc, timestamp_local, lat, lon,
             alt, speed, accuracy, battery))
        new_id = cur.lastrowid
        if keep_latest_only:
            conn.execute(
                "DELETE FROM gpsdata WHERE Device_Id = ? AND Id <> ?",
                (device_id, new_id))
        if own:
            conn.commit()
        log.info("gps fix stored for %s: lat=%s lon=%s acc=%s batt=%s",
                 device_id, lat, lon, accuracy, battery)
        return new_id
    finally:
        if own:
            conn.close()


def get_latest(device_id, conn=None):
    """Latest fix for one device as a dict, or None."""
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM gpsdata WHERE Device_Id = ? "
            "ORDER BY Id DESC LIMIT 1", (device_id,)).fetchone()
    finally:
        if own:
            conn.close()
    return _row_to_dict(row) if row else None


def get_latest_all(conn=None):
    """Latest fix per device (one row each) as a list of dicts.

    Joins device label so the map can show the label + timestamp + battery.
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        rows = conn.execute(
            "SELECT g.*, d.Device_Label AS Device_Label, d.Is_Own AS Is_Own "
            "FROM gpsdata g "
            "JOIN ( SELECT Device_Id, MAX(Id) AS MaxId FROM gpsdata "
            "       GROUP BY Device_Id ) m ON g.Id = m.MaxId "
            "LEFT JOIN devices d ON d.Device_Id = g.Device_Id"
        ).fetchall()
    finally:
        if own:
            conn.close()
    result = []
    for r in rows:
        d = _row_to_dict(r)
        d["device_label"] = r["Device_Label"]
        d["is_own"] = int(r["Is_Own"]) if r["Is_Own"] is not None else 0
        result.append(d)
    return result


def _row_to_dict(row):
    return {
        "id": row["Id"],
        "device_id": row["Device_Id"],
        "timestamp_utc": row["Timestamp_utc"],
        "timestamp_local": row["Timestamp_local"],
        "latitude": row["Latitude"],
        "longitude": row["Longitude"],
        "altitude": row["Altitude"],
        "speed": row["Speed"],
        "accuracy": row["Accuracy"],
        "battery_level": row["Battery_level"],
    }
