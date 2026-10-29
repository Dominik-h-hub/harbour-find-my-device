#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fixed_location.py -- Daemon side of "GPS at Home" (see fmd/fixedloc.py).

Asks ConnMan (system bus) for the connected WiFi networks -- something only
the unsandboxed daemons may do -- and mirrors them into the runtime table so
the sandboxed GUI can apply the same rule in its foreground poll.
"""

import logging

from fmd import fixedloc, runtime

log = logging.getLogger("fmd.fixedloc")

ACCURACY_M = fixedloc.ACCURACY_M
SPEED = fixedloc.SPEED

# Last value written to the runtime table; only changes are written.
_published = {"value": None}


def connected_wifi_names():
    """SSIDs of the WiFi services ConnMan reports as connected; empty on error."""
    try:
        import dbus
        mgr = dbus.Interface(dbus.SystemBus().get_object("net.connman", "/"),
                             "net.connman.Manager")
        return [str(props.get("Name", ""))
                for _path, props in mgr.GetServices()
                if str(props.get("Type", "")) == "wifi"
                and str(props.get("State", "")) in ("ready", "online")]
    except Exception as exc:
        log.warning("could not query WiFi state: %s", exc)
        return []


# Last decision logged by get(); only changes are logged.
_last_decision = {"value": None}


def get():
    """(lat, lon) to send instead of a GPS fix, or None."""
    seen = {}

    def names():
        seen["names"] = connected_wifi_names()
        return seen["names"]

    fixed = fixedloc.match(names)
    # Log the decision on a change only (SSIDs stay out of the log).
    if fixed is not None:
        decision = "match"
    elif "names" not in seen:
        decision = "not configured"
    else:
        decision = "configured WiFi not connected (%d WiFi connected)" % len(
            seen["names"])
    if decision != _last_decision["value"]:
        _last_decision["value"] = decision
        log.info("GPS at Home: %s", decision)
    return fixed


def publish_wifi_state():
    """Mirror the connected SSIDs for the GUI (only while the feature is
    configured; cleared otherwise). Writes the DB only on a change."""
    names = connected_wifi_names() if fixedloc.configured(warn=False) else []
    value = "\n".join(sorted(names))
    if value == _published["value"]:
        return
    try:
        runtime.set_connected_wifi(names)
        _published["value"] = value
    except Exception:
        log.exception("could not publish WiFi state")
