#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ring_control.py -- Make the device ring loudly for the RING command.

The ring plays the user-selected ringtone file on a loop via GStreamer (playbin),
bounded to ~60 seconds and stopped cleanly. Volume is pushed to max via the
profiled D-Bus interface so a silenced device still rings. If the ringtone file
cannot be played a raw sine tone is used as a last resort.

Run from the (user) command daemon. Never raises; returns True if a ring was
started.
"""

import logging
import os
import threading
import time
from fmd import paths, settings

log = logging.getLogger("fmd.ring")

RING_SECONDS = 60

# Cross-process ring state. The command daemon runs the actual ring, but the UI
# process needs to know the own device is ringing so it can show the STOP button
# on the own-device row. We share that fact through a tiny file holding the
# ring's expiry timestamp; any process can check is_ringing().
_STATE_FILE = "ring_active"

PROFILED_NAME = "com.nokia.profiled"
PROFILED_PATH = "/com/nokia/profiled"
PROFILED_IFACE = "com.nokia.profiled"

_active = {"stop": None}
_preview = {"stop": None, "timer": None}


def _state_path():
    return os.path.join(paths.data_dir(), _STATE_FILE)


def _write_state(duration):
    """Record that a ring is active until now+duration (cross-process flag)."""
    try:
        with open(_state_path(), "w") as fh:
            fh.write(str(time.time() + duration))
    except OSError as exc:
        log.info("could not write ring state: %s", exc)


def _clear_state():
    try:
        os.remove(_state_path())
    except OSError:
        pass


def is_ringing():
    """True if a ring is currently active on this device. Lets the UI process show
    the STOP button for a ring the command daemon started. Self-healing: an expired
    or stale state file is treated as 'not ringing' and removed."""
    try:
        with open(_state_path()) as fh:
            until = float(fh.read().strip() or "0")
    except (OSError, ValueError):
        return False
    if time.time() < until:
        return True
    _clear_state()
    return False


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


def _gst_init():
    """Import and initialise GStreamer, or return None if unavailable."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        return Gst
    except Exception as exc:
        log.warning("GStreamer not available: %s", exc)
        return None


def _play_file(path, loop, volume=None):
    """Play a sound file via playbin.

    `volume` is the playbin stream volume (0.0..1.0). Pass None to leave it
    untouched so the stream follows the system media volume -- setting it forces
    the PulseAudio stream to that level and overrides the user's volume keys, which
    we only want for a real (loud) ring, not for the Settings preview.

    Returns a stop() callable, or None if the file/pipeline could not be set up."""
    Gst = _gst_init()
    if Gst is None:
        return None
    if not path or not os.path.isfile(path):
        log.warning("ringtone file not found: %s", path)
        return None
    try:
        playbin = Gst.ElementFactory.make("playbin", None)
        if playbin is None:
            log.warning("could not create playbin element")
            return None
        playbin.set_property("uri", "file://" + os.path.abspath(path))
        if volume is not None:
            try:
                playbin.set_property("volume", volume)
            except Exception:
                pass
        bus = playbin.get_bus()
        playbin.set_state(Gst.State.PLAYING)
        log.info("ringtone playing: %s (loop=%s)", path, loop)

        stop_event = threading.Event()

        def watch():
            while not stop_event.is_set():
                msg = bus.timed_pop_filtered(
                    100 * Gst.MSECOND,
                    Gst.MessageType.EOS | Gst.MessageType.ERROR)
                if msg is None:
                    continue
                if msg.type == Gst.MessageType.ERROR:
                    err, _dbg = msg.parse_error()
                    log.warning("ringtone playback error: %s", err)
                    break
                # EOS: loop by seeking to the start, or finish.
                if loop and not stop_event.is_set():
                    playbin.seek_simple(
                        Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
                else:
                    break

        threading.Thread(target=watch, name="ring-bus", daemon=True).start()

        def stop():
            stop_event.set()
            try:
                playbin.set_state(Gst.State.NULL)
            except Exception:
                pass
        return stop
    except Exception as exc:
        log.warning("ringtone playback failed (%s): %s", path, exc)
        return None


def _play_sine():
    #Fallback: a raw 1 kHz sine, no media files needed. Returns a stop() callable or None.
    Gst = _gst_init()
    if Gst is None:
        return None
    try:
        pipeline = Gst.parse_launch(
            "audiotestsrc wave=sine freq=1000 volume=0.9 "
            "! audioconvert ! autoaudiosink")
        pipeline.set_state(Gst.State.PLAYING)
        log.info("sine fallback ring started")

        def stop():
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        return stop
    except Exception as exc:
        log.warning("sine fallback failed: %s", exc)
        return None


def ring(duration=RING_SECONDS):
    #Start ringing for `duration` seconds in a background timer. Returns bool.
    stop_current()
    _force_volume_max()
    tone = settings.get(settings.RING_TONE) or ""
    # lost/silenced device still rings loudly.
    stop = _play_file(tone, loop=True, volume=1.0) if tone else None
    if stop is None:
        log.warning("falling back to sine tone (tone=%r unplayable)", tone)
        stop = _play_sine()
    if stop is None:
        log.error("no ring mechanism available")
        return False

    _active["stop"] = stop
    _write_state(duration)
    timer = threading.Timer(duration, stop_current)
    timer.daemon = True
    timer.start()
    log.info("ringing for %ds (tone=%s)", duration, tone or "sine")
    return True


def stop_current():
    """Stop any active ring."""
    _clear_state()
    stop = _active.get("stop")
    if stop:
        try:
            stop()
        except Exception:
            pass
        _active["stop"] = None
        log.info("ring stopped")

def preview(path, duration=6):
    """Play `path` once for up to `duration` seconds for the Settings audition.
    Independent of a real ring (own _preview slot). Unlike a real ring it does NOT
    force the loud profile / max volume -- it just auditions at the current volume.
    Returns bool."""
    stop_preview()
    stop = _play_file(path, loop=False)
    if stop is None:
        return False
    _preview["stop"] = stop
    timer = threading.Timer(duration, stop_preview)
    timer.daemon = True
    _preview["timer"] = timer
    timer.start()
    return True


def stop_preview():
    """Stop a running Settings preview (and cancel its auto-stop timer)."""
    timer = _preview.get("timer")
    if timer is not None:
        timer.cancel()
        _preview["timer"] = None
    stop = _preview.get("stop")
    if stop:
        try:
            stop()
        except Exception:
            pass
        _preview["stop"] = None


def _main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if ring(10):
        time.sleep(12)


if __name__ == "__main__":
    _main()
