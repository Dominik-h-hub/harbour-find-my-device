#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify.py -- Local push notifications to the lock screen.
For every executed remote action notifies the device owner
via a local push notification (Nemo Notification API). From a headless user
service the simplest reliable path is the freedesktop Notifications D-Bus service
on the USER SESSION bus, which Lipstick implements on Sailfish OS.
"""

import logging
import os

log = logging.getLogger("fmd.notify")

_FDN_NAME = "org.freedesktop.Notifications"
_FDN_PATH = "/org/freedesktop/Notifications"
_FDN_IFACE = "org.freedesktop.Notifications"

APP_NAME = "harbour-find-my-device"

# The app logo, installed by the app package
_APP_ICON_SIZES = ("172x172", "128x128", "108x108", "86x86")
_APP_ICON_PATH = "/usr/share/icons/hicolor/{}/apps/" + APP_NAME + ".png"
# Theme icon used when the app package (and with it the logo) is not installed.
_FALLBACK_ICON = "icon-lock-information"


def _app_icon():
    for size in _APP_ICON_SIZES:
        path = _APP_ICON_PATH.format(size)
        if os.path.isfile(path):
            return path
    return _FALLBACK_ICON


def notify(summary, body="", icon=None):
    #Post a local notification. Returns the notification id (int) or 0.
    icon = icon or _app_icon()
    try:
        import dbus
        bus = dbus.SessionBus()
        obj = bus.get_object(_FDN_NAME, _FDN_PATH)
        iface = dbus.Interface(obj, _FDN_IFACE)
        hints = {
            # Nemo categories drive lock-screen presentation.
            "x-nemo-icon": icon,
            "category": "x-nemo.general",
        }
        nid = iface.Notify(
            APP_NAME,            # app_name
            dbus.UInt32(0),      # replaces_id
            icon,                # app_icon
            str(summary),        # summary
            str(body),           # body
            dbus.Array([], signature="s"),   # actions
            hints,               # hints
            dbus.Int32(-1))      # expire_timeout (default)
        log.info("notification posted: %s", summary)
        return int(nid)
    except Exception as exc:
        log.warning("could not post notification (%s): %s", summary, exc)
        return 0
