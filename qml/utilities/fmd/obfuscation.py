#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""obfuscation.py -- Reversible obfuscation for locally stored secrets.

IMPORTANT SECURITY NOTE:
  This is OBFUSCATION, not encryption. The key is embedded in the source, so
  anyone with read access to the app files AND the database can trivially
  recover the plaintext. This is deliberately NOT using the Sailfish OS
  Encryption API. The goal is only to keep PINs / passwords / TOTP secrets from
  appearing as clear text in the DB (e.g. casual inspection, backups, logs).

Format: stored values are prefixed with a marker so we can tell obfuscated from
plain values and migrate later if needed:  "obf1:" + base64(XOR(plaintext)).
deobfuscate() returns plain values unchanged if they lack the marker, which makes
the functions safe to apply idempotently and tolerant of older/hand-edited rows.
"""

import base64
import logging

log = logging.getLogger("fmd.obf")

_MARKER = "obf1:"

# Embedded XOR key.
_KEY = b"harbour-find-my-device/obfuscation-key/v1"


def _xor(data):
    klen = len(_KEY)
    return bytes(b ^ _KEY[i % klen] for i, b in enumerate(data))


def obfuscate(plaintext):
    """Obfuscate a string for storage. None/empty pass through unchanged.

    Idempotent: an already-obfuscated value is returned as-is.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    if isinstance(plaintext, str) and plaintext.startswith(_MARKER):
        return plaintext  # already obfuscated
    raw = str(plaintext).encode("utf-8")
    enc = base64.b64encode(_xor(raw)).decode("ascii")
    return _MARKER + enc


def deobfuscate(stored):
    """Recover a string stored by obfuscate(). Plain (unmarked) values pass through."""
    if stored is None or stored == "":
        return stored
    s = str(stored)
    if not s.startswith(_MARKER):
        return s  # tolerate plain / legacy values
    try:
        raw = base64.b64decode(s[len(_MARKER):].encode("ascii"))
        return _xor(raw).decode("utf-8")
    except Exception as exc:
        log.warning("deobfuscate failed, returning raw value: %s", exc)
        return s
