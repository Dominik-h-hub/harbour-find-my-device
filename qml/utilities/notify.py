#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify.py -- Local push notifications to the lock screen.
For every executed remote action notifies the device owner
via a local push notification (Nemo Notification API). From a headless user
service the simplest reliable path is the freedesktop Notifications D-Bus service
on the USER SESSION bus, which Lipstick implements on Sailfish OS.
"""

import logging

log = logging.getLogger("fmd.notify")

_FDN_NAME = "org.freedesktop.Notifications"
_FDN_PATH = "/org/freedesktop/Notifications"
_FDN_IFACE = "org.freedesktop.Notifications"

APP_NAME = "harbour-find-my-device"


def notify(summary, body="", icon="icon-lock-information"):
    #Post a local notification. Returns the notification id (int) or 0.
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
