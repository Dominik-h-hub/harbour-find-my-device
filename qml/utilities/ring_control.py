#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ring_control.py -- Make the device ring loudly for the RING command.

The ring is bounded to ~60 seconds and stopped cleanly. Volume is pushed to max
via the profile D-Bus interface so a silenced device still rings.

Run from the (user) command daemon. Never raises; returns True if a ring was
started.
"""

import logging
import threading
import time

log = logging.getLogger("fmd.ring")

RING_SECONDS = 60

NGF_NAME = "com.nokia.NonGraphicFeedback1.Backend"
NGF_PATH = "/com/nokia/NonGraphicFeedback1"
NGF_IFACE = "com.nokia.NonGraphicFeedback1"

PROFILED_NAME = "com.nokia.profiled"
PROFILED_PATH = "/com/nokia/profiled"
PROFILED_IFACE = "com.nokia.profiled"

_active = {"stop": None}


def _force_volume_max():
    """Best-effort: raise ringtone/system volume so a silent device still rings."""
    try:
        import dbus
        bus = dbus.SessionBus()
        prof = dbus.Interface(bus.get_object(PROFILED_NAME, PROFILED_PATH),
                              PROFILED_IFACE)
        # Switch to the 'general' (loud) profile and max the ringtone volume.
        try:
            prof.set_profile("general")
        except Exception:
            pass
        for key in ("ringing.alert.volume", "system.sound.level.volume"):
            try:
                prof.set_value("general", key, "100")
            except Exception:
                pass
        log.info("forced loud profile / max volume (best-effort)")
    except Exception as exc:
        log.info("could not force volume: %s", exc)


def _ring_ngf():
    """Play the ringtone via ngfd. Returns a stop() callable or None."""
    try:
        import dbus
        bus = dbus.SessionBus()
        ngf = dbus.Interface(bus.get_object(NGF_NAME, NGF_PATH), NGF_IFACE)
        props = dbus.Dictionary({"media.audio": dbus.Boolean(True)},
                                signature="sv")
        event_id = ngf.Play("ringtone", props)
        log.info("ngf ringtone started (id=%s)", event_id)

        def stop():
            try:
                ngf.Stop(dbus.UInt32(event_id))
            except Exception as exc:
                log.info("ngf stop failed: %s", exc)
        return stop
    except Exception as exc:
        log.info("ngf ring unavailable: %s", exc)
        return None


def _ring_gst():
    """Fallback: loop a tone with GStreamer. Returns a stop() callable or None."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        # audiotestsrc gives a guaranteed-present loud tone, no media files needed.
        pipeline = Gst.parse_launch(
            "audiotestsrc wave=sine freq=1000 volume=0.9 ! audioconvert ! autoaudiosink")
        pipeline.set_state(Gst.State.PLAYING)
        log.info("gstreamer ring started")

        def stop():
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        return stop
    except Exception as exc:
        log.warning("gstreamer ring fallback failed: %s", exc)
        return None


def ring(duration=RING_SECONDS):
    """Start ringing for `duration` seconds in a background timer. Returns bool."""
    stop_current()
    _force_volume_max()
    stop = _ring_ngf() or _ring_gst()
    if stop is None:
        log.error("no ring mechanism available")
        return False

    _active["stop"] = stop
    timer = threading.Timer(duration, stop_current)
    timer.daemon = True
    timer.start()
    log.info("ringing for %ds", duration)
    return True


def stop_current():
    """Stop any active ring."""
    stop = _active.get("stop")
    if stop:
        try:
            stop()
        except Exception:
            pass
        _active["stop"] = None
        log.info("ring stopped")


def _main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if ring(10):
        time.sleep(12)


if __name__ == "__main__":
    _main()
