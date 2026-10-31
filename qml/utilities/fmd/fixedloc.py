#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fixedloc.py -- "GPS at Home": fixed coordinates instead of a GPS fix.

When both settings (WiFi name + "lat, lon") are filled and the phone is
connected to that WiFi network, every location path -- the GPS command
(daemon_cmd), the periodic GPS cycle (daemon_gps) and the app's foreground
poll / manual refresh (api.py) -- uses these coordinates instead of querying
GPS. In every other case the normal GPS flow runs unchanged.

Only the unsandboxed daemons can ask ConnMan for the connected WiFi (the GUI
must not use dbus). The cmd daemon therefore mirrors the connected SSIDs into
the runtime table (runtime.set_connected_wifi); the GUI reads them from there.
"""

import logging

from . import settings

log = logging.getLogger("fmd.fixedloc")

# Entered coordinates are by definition
ACCURACY_M = 1.0
SPEED = 0.0


def configured(warn=True):
    """(ssid, (lat, lon)) when both settings are filled and valid, else None.
    warn=False for periodic callers, so bad input does not flood the log."""
    ssid = (settings.get(settings.FIXED_LOCATION_WIFI) or "").strip()
    coords = (settings.get(settings.FIXED_LOCATION_COORDS) or "").strip()
    if not ssid or not coords:
        return None
    try:
        lat, lon = [float(p.strip()) for p in coords.split(",")]
    except ValueError:
        if warn:
            log.warning("fixed location coordinates invalid (expected "
                        "'lat, lon'); using GPS")
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        if warn:
            log.warning("fixed location coordinates out of range; using GPS")
        return None
    return ssid, (lat, lon)


def match(connected_ssids):
    """(lat, lon) to use instead of a GPS fix, or None.

    `connected_ssids` is a callable returning the currently connected WiFi
    names; it is only called when the feature is configured."""
    conf = configured()
    if conf is None:
        return None
    ssid, coords = conf
    if ssid not in connected_ssids():
        return None
    return coords
