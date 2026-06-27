#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fmd -- backend package for harbour-find-my-device.

Shared by the QML bridge (api.py) and the two background daemons. Keeps all
SQLite, settings, secret-obfuscation, device and token logic in one place so the
UI and the daemons operate on exactly the same data and rules.
"""

__all__ = [
    "paths",
    "obfuscation",
    "db",
    "settings",
    "devices",
    "tokens",
    "gpsstore",
]
