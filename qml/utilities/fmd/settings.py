#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings.py -- App settings stored in the `settings` key/value table.

The settings live in the same SQLite DB as the data so the daemons can read the
feature toggles without a second config mechanism. Secret-valued keys (passwords)
are obfuscated on write and de-obfuscated on read (see obfuscation.py). Booleans
are stored as "1"/"0" strings; everything is text in the table.

Note: the own device's PIN and TOTP secret are NOT here -- they belong to the
own-device row in the `devices` table (devices.py), because a remote device also
has a per-row PIN. Settings only references them through the UI layer.
"""

import logging

from . import db
from .obfuscation import deobfuscate, obfuscate

log = logging.getLogger("fmd.settings")

# --- keys ------------------------------------------------------------------
# General
DEVICE_LABEL = "device_label"
GPS_INTERVAL_MIN = "gps_interval_min"
AUTO_ENABLE_LOCATION = "auto_enable_location"
# MQTT
MQTT_ENABLED = "mqtt_enabled"
MQTT_SERVER = "mqtt_server"
MQTT_TLS = "mqtt_tls"
MQTT_USERNAME = "mqtt_username"
MQTT_PASSWORD = "mqtt_password"
MQTT_PORT = "mqtt_port"
BACKGROUND_ENABLED = "background_enabled"
# Remote actions
RING_ENABLED = "ring_enabled"
RING_TONE = "ring_tone"                   # absolute path to the ringtone .ogg file
LOCK_ENABLED = "lock_enabled"
DELETE_ENABLED = "delete_enabled"
# Camera
CAMERA_ENABLED = "camera_enabled"
WEBDAV_URL = "webdav_url"
WEBDAV_USERNAME = "webdav_username"
WEBDAV_PASSWORD = "webdav_password"
# SMS
SMS_REMOTE_ENABLED = "sms_remote_enabled"
SMS_GPS_ENABLED = "sms_gps_enabled"
SMS_WHITELIST = "sms_whitelist"
# Map
TILE_PROVIDER = "tile_provider"          # "osm" | "geoapify"
GEOAPIFY_KEY = "geoapify_key"

# Keys whose values are obfuscated at rest.
_SECRET_KEYS = frozenset({MQTT_PASSWORD, WEBDAV_PASSWORD})

# Keys additionally masked in log output only (stored in clear -- do not move
# them into _SECRET_KEYS, that would garble existing installs' stored values).
# Users share logs for support, so keep API keys and phone numbers out of them.
_LOG_MASKED_KEYS = _SECRET_KEYS | {GEOAPIFY_KEY, SMS_WHITELIST}

# Defaults applied when a key is missing.
DEFAULTS = {
    DEVICE_LABEL: "",
    GPS_INTERVAL_MIN: "5",
    AUTO_ENABLE_LOCATION: "0",
    MQTT_ENABLED: "0",
    MQTT_SERVER: "",
    MQTT_TLS: "1",
    MQTT_USERNAME: "",
    MQTT_PASSWORD: "",
    MQTT_PORT: "8883",
    BACKGROUND_ENABLED: "0",
    RING_ENABLED: "0",
    # A pleasant default ringtone; the harsh sine is only a last-resort fallback.
    RING_TONE: "/usr/share/sounds/jolla-ringtones/stereo/jolla-ringtone.ogg",
    LOCK_ENABLED: "0",
    DELETE_ENABLED: "0",
    CAMERA_ENABLED: "0",
    WEBDAV_URL: "",
    WEBDAV_USERNAME: "",
    WEBDAV_PASSWORD: "",
    SMS_REMOTE_ENABLED: "0",
    SMS_GPS_ENABLED: "0",
    SMS_WHITELIST: "",
    TILE_PROVIDER: "osm",
    GEOAPIFY_KEY: "",
}


# --- low-level access ------------------------------------------------------
def get(key, default=None, conn=None):
    """Return a setting's plain string value (de-obfuscated if a secret key)."""
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute("SELECT Value FROM settings WHERE Key = ?", (key,)).fetchone()
    finally:
        if own:
            conn.close()
    if row is None:
        return default if default is not None else DEFAULTS.get(key)
    value = row["Value"]
    if key in _SECRET_KEYS:
        return deobfuscate(value)
    return value


def set(key, value, conn=None):
    """Persist a setting. Secret keys are obfuscated before storage. Logs the write."""
    stored = obfuscate(value) if key in _SECRET_KEYS else (
        "" if value is None else str(value))
    own = conn is None
    conn = conn or db.connect()
    try:
        conn.execute(
            "INSERT INTO settings (Key, Value) VALUES (?, ?) "
            "ON CONFLICT(Key) DO UPDATE SET Value = excluded.Value",
            (key, stored))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    # Never log secret values in clear.
    shown = "***" if key in _LOG_MASKED_KEYS else stored
    log.info("setting saved: %s = %s", key, shown)


def set_many(mapping):
    """Persist several settings in one transaction."""
    with db.connection() as conn:
        for key, value in mapping.items():
            set(key, value, conn=conn)


# --- typed helpers ---------------------------------------------------------
def get_bool(key, conn=None):
    return str(get(key, conn=conn)).strip() in ("1", "true", "True", "yes", "on")


def get_int(key, fallback=0, conn=None):
    try:
        return int(str(get(key, conn=conn)).strip())
    except (TypeError, ValueError):
        return fallback


def get_all_public(conn=None):
    """Return every setting as a dict for the UI. Secret VALUES are de-obfuscated
    (the UI needs to display/edit them); callers must treat them as sensitive.
    """
    result = dict(DEFAULTS)
    own = conn is None
    conn = conn or db.connect()
    try:
        for row in conn.execute("SELECT Key, Value FROM settings"):
            key = row["Key"]
            value = row["Value"]
            result[key] = deobfuscate(value) if key in _SECRET_KEYS else value
    finally:
        if own:
            conn.close()
    return result
