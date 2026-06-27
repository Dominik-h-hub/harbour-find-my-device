#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tokens.py -- Auth tokens for remote commands.

Two channels, two mechanisms (per the requirements "PIN Details"):

  MQTT  -> command-bound HMAC token. Secret = the device PIN. The token is bound
           to the command (a LOCK token cannot be replayed as DELETE) and only
           valid for a short time window (replay protection). The signed message
           includes the command, its arg, and a coarse time bucket; the receiver
           accepts the current bucket +/-1 to tolerate clock drift.

  SMS   -> TOTP one-time code OR a one-time backup code, both accepted by the
           target device. The sender whitelist is the binding second factor, so
           SMS codes are NOT command-bound. Backup codes are stored hashed and
           consumed atomically.

Pure standard library (hmac/hashlib/secrets/base64/struct) -- no extra deps, so
it works under the Sailfish python3 / Qt 5.6.3 toolchain.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time

from . import db, devices
from .obfuscation import obfuscate

log = logging.getLogger("fmd.tokens")

# --- MQTT command HMAC -----------------------------------------------------
HMAC_STEP = 30           # seconds per time bucket
HMAC_WINDOW = 1          # accept current bucket +/- this many
_HMAC_DIGITS = 16        # length of the hex token we emit


def _hmac_message(cmd, arg, bucket):
    # arg is normalized so "CAMERA" with default back matches both ends.
    return "{}:{}:{}".format((cmd or "").upper(), (arg or "").lower(), bucket).encode()


def make_command_token(secret, cmd, arg=None, when=None):
    """Build the HMAC token a publisher sends in the cmd payload.

    secret : the remote device's PIN (the app signs OUTGOING commands with it).
    Returns a short hex string bound to cmd+arg+current-time-bucket.
    """
    when = when if when is not None else time.time()
    bucket = int(when // HMAC_STEP)
    mac = hmac.new(str(secret).encode(), _hmac_message(cmd, arg, bucket),
                   hashlib.sha256).hexdigest()
    return mac[:_HMAC_DIGITS]


def verify_command_token(secret, cmd, arg, token, when=None):
    """Verify an incoming MQTT command token against our PIN.

    Accepts the current time bucket +/- HMAC_WINDOW. Constant-time compare.
    Returns True/False.
    """
    if not secret or not token:
        return False
    when = when if when is not None else time.time()
    base = int(when // HMAC_STEP)
    for w in range(-HMAC_WINDOW, HMAC_WINDOW + 1):
        mac = hmac.new(str(secret).encode(),
                       _hmac_message(cmd, arg, base + w),
                       hashlib.sha256).hexdigest()[:_HMAC_DIGITS]
        if hmac.compare_digest(mac, str(token)):
            return True
    return False


# --- TOTP (SMS channel) ----------------------------------------------------
TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1


def generate_totp_secret(length=20):
    """Generate a random Base32 TOTP secret (own device, SMS channel).

    This is NOT the PIN -- it's a separate secret shown in Settings (QR + text)
    to enrol in an authenticator app on a SECOND device.
    """
    raw = secrets.token_bytes(length)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def totp_now(secret_b32, when=None, step=TOTP_STEP, digits=TOTP_DIGITS):
    """Current TOTP code for a secret (used for testing / display)."""
    key = base64.b32decode(_pad_b32(secret_b32), casefold=True)
    counter = int((when if when is not None else time.time()) // step)
    return _hotp(key, counter, digits)


def verify_totp(secret_b32, code, step=TOTP_STEP, digits=TOTP_DIGITS,
                window=TOTP_WINDOW, when=None):
    """RFC 6238 TOTP verification (stdlib). Accepts +/- window steps."""
    try:
        key = base64.b32decode(_pad_b32(secret_b32), casefold=True)
    except Exception:
        return False
    code = str(code).strip()
    now = when if when is not None else time.time()
    for w in range(-window, window + 1):
        if _hotp(key, int(now // step) + w, digits) == code:
            return True
    return False


def totp_uri(secret_b32, account, issuer="FindMyDevice"):
    """otpauth:// URI for a QR code, to enrol in an authenticator app."""
    return ("otpauth://totp/{issuer}:{account}?secret={secret}"
            "&issuer={issuer}&period={step}&digits={digits}").format(
                issuer=issuer, account=account, secret=secret_b32,
                step=TOTP_STEP, digits=TOTP_DIGITS)


def _hotp(key, counter, digits):
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    val = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(val).zfill(digits)


def _pad_b32(secret_b32):
    s = str(secret_b32).strip().upper().replace(" ", "")
    pad = (-len(s)) % 8
    return s + ("=" * pad)


# --- own-device TOTP secret storage ---------------------------------------
def set_own_totp_secret(secret_b32, conn=None):
    """Store (obfuscated) a TOTP secret on the own-device row."""
    own = conn is None
    conn = conn or db.connect()
    try:
        dev = devices.ensure_own_device(conn=conn)
        conn.execute("UPDATE devices SET Totp_Secret = ? WHERE Device_Id = ?",
                     (obfuscate(secret_b32), dev["device_id"]))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    log.info("own-device TOTP secret stored")


# --- backup codes (SMS channel fallback) -----------------------------------
BACKUP_CODE_COUNT = 10
_BACKUP_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # no ambiguous chars


def _normalize_code(code):
    """Normalize a backup code for hashing/compare: upper, strip separators."""
    return "".join(ch for ch in str(code).upper() if ch in _BACKUP_ALPHABET)


def _hash_code(code):
    return hashlib.sha256(_normalize_code(code).encode()).hexdigest()


def generate_backup_codes(device_id=None, count=BACKUP_CODE_COUNT, conn=None):
    """Regenerate backup codes for the own device.

    Deletes existing codes first (avoid mixed old/new state), inserts the new
    hashes, and returns the PLAINTEXT codes ONCE for display. Plaintext is never
    stored. Returns a list of formatted codes (e.g. "ABCD-EFGH").
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        if device_id is None:
            device_id = devices.own_device_id(conn=conn)
        now = devices.iso_utc()
        plain_codes = []
        conn.execute("DELETE FROM backup_codes WHERE Device_Id = ?", (device_id,))
        for _ in range(count):
            raw = "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(8))
            pretty = raw[:4] + "-" + raw[4:]
            plain_codes.append(pretty)
            conn.execute(
                "INSERT INTO backup_codes (Device_Id, Code_Hash, Created_Utc) "
                "VALUES (?, ?, ?)", (device_id, _hash_code(raw), now))
        if own:
            conn.commit()
        log.info("generated %d backup codes for %s", count, device_id)
        return plain_codes
    finally:
        if own:
            conn.close()


def count_unused_backup_codes(device_id=None, conn=None):
    own = conn is None
    conn = conn or db.connect()
    try:
        if device_id is None:
            device_id = devices.own_device_id(conn=conn)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM backup_codes WHERE Device_Id = ? AND Used = 0",
            (device_id,)).fetchone()
        return int(row["n"]) if row else 0
    finally:
        if own:
            conn.close()


def consume_backup_code(code, device_id=None, conn=None):
    """Atomically consume a backup code. Returns True if a valid unused code was
    marked used (changes()==1), False otherwise (wrong or already used).
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        if device_id is None:
            device_id = devices.own_device_id(conn=conn)
        cur = conn.execute(
            "UPDATE backup_codes "
            "SET Used = 1, Used_Utc = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
            "WHERE Device_Id = ? AND Code_Hash = ? AND Used = 0",
            (device_id, _hash_code(code)))
        if own:
            conn.commit()
        ok = cur.rowcount == 1
        log.info("backup code consume for %s -> %s", device_id, ok)
        return ok
    finally:
        if own:
            conn.close()


def verify_sms_code(code, device_id=None, conn=None):
    """SMS second factor: accept a valid TOTP code OR consume a backup code.

    TOTP is checked first (non-destructive); only if that fails do we try to
    consume a backup code. Returns True on success.
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        if device_id is None:
            device_id = devices.own_device_id(conn=conn)
        dev = devices.get_device(device_id, conn=conn)
        secret = dev.get("totp_secret") if dev else None
        if secret and verify_totp(secret, code):
            log.info("SMS code accepted via TOTP")
            return True
        if consume_backup_code(code, device_id=device_id, conn=conn):
            # consume happened on our (possibly owned) connection; persist it.
            if own:
                conn.commit()
            log.info("SMS code accepted via backup code")
            return True
        log.warning("SMS code rejected (neither TOTP nor backup matched)")
        return False
    finally:
        if own:
            conn.close()
