#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daemon_gps.py -- GPS background service (systemd USER service).

Periodically obtains the own-device position, stores the latest fix in SQLite and
(when enabled and online) publishes it to fmd/<device-id> over MQTT.

Runs in the user session, where geoclue lives (see GPS_NOTES.md), so it can read
a fix directly. It starts at boot and is never started/stopped from outside:
while the "Background activity" switch is off it IDLES (short poll timer),
re-reading the toggles from the SQLite DB each tick. The UI signals settings
changes by bumping the settings generation counter (fmd/runtime.py); a
heartbeat record tells the UI whether the daemon is running/idle at all.

MQTT is connect-per-tick: the publisher subscribes to nothing, so a standing
connection buys nothing but is exactly the part that does not survive a
WLAN<->mobile handover. The TLS handshake rides on a radio wakeup that happens
anyway (the GPS fix just ran), and between ticks there is zero network
traffic. Delivery is verified (PUBACK) by mqtt_client._publish; a fix that
cannot be delivered (no route at all) is kept pending and re-sent on the next
cycle -- with retain=True only the newest position matters.

ExecStart: python3 /usr/share/harbour-find-my-device-daemon/daemon_gps.py
"""

import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

# Make sibling modules + the fmd package importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fmd import db, devices, gpsstore, runtime, settings
import location_control
import mqtt_client

log = logging.getLogger("fmd.daemon.gps")

IDLE_POLL_SECONDS = 30        # how often to re-check toggles while idle
CONNECT_TIMEOUT_S = 15        # CONNACK wait per connection attempt

# Set by SIGTERM/SIGINT; every wait blocks on this event instead of polling a
# flag once per second (300 wakeups per 5-min cycle cost more battery than
# everything else in this daemon combined).
_stop = threading.Event()

# Version of the installed daemon package; written into every heartbeat so the
# UI can offer an update when the bundled RPM is newer (see the app's Settings).
VERSION_FILE = "/usr/share/harbour-find-my-device-daemon/VERSION"
DAEMON_VERSION = ""


def _handle_signal(signum, _frame):
    log.info("signal %s received, shutting down", signum)
    _stop.set()


class _Heartbeat(object):
    """Keeps the GPS daemon's heartbeat fresh from its own thread.

    The beat must not depend on the main loop making progress: one cycle
    blocks for up to ~92s in the fix wait plus another ~30s in the MQTT
    publish, while the UI already treats a heartbeat older than
    runtime.HEARTBEAT_FRESH_S (90s) as "daemon stopped". Tying the beat to the
    loop therefore made the Settings page flash "not installed" in the middle
    of every tick.

    Only the *content* follows the loop: set() records the state and the
    settings generation it was built from, the thread just re-stamps it.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = "idle"
        self._generation = None
        self._thread = None

    def set(self, state, generation):
        """Record what the loop is doing now and beat immediately."""
        with self._lock:
            self._state = state
            self._generation = generation
        self._write()

    def _write(self):
        with self._lock:
            state, generation = self._state, self._generation
        runtime.write_heartbeat("gps", state, DAEMON_VERSION, generation)

    def _run(self):
        # wait() returns True once _stop is set -> loop ends without a delay.
        while not _stop.wait(runtime.HEARTBEAT_INTERVAL_S):
            try:
                self._write()
            except Exception:
                log.exception("heartbeat write failed; continuing")

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()


def _abort_on_settings_change(generation, every_s=3.0):
    """Build a should_abort predicate for gps_reader.get_fix().

    Without it a settings save waits out the whole fix timeout before the
    daemon reacts. The predicate runs once per GLib iteration, so the
    generation -- one DB read -- is throttled; _stop is free to check.
    """
    state = {"next": 0.0, "aborted": False}

    def check():
        if _stop.is_set() or state["aborted"]:
            return True
        now = time.time()
        if now < state["next"]:
            return False
        state["next"] = now + every_s
        try:
            if runtime.generation() != generation:
                log.info("settings changed; aborting the fix wait")
                state["aborted"] = True
                return True
        except Exception:
            log.exception("generation check failed during the fix wait")
        return False

    return check


class GpsDaemon(object):
    def __init__(self):
        # Newest fix that could not be delivered (no route during a handover):
        # (own_id, payload). Only the newest is kept -- retain=True makes older
        # positions worthless. Second line of defence behind the in-tick
        # reconnect+republish of mqtt_client._publish.
        self._pending = None

    # -- mqtt --
    def _publish(self, own_id, payload):
        """Connect, publish verified, disconnect. Returns True when the fix is
        on the broker (or publishing is not configured -> nothing to deliver).

        No network_up() pre-probe: the connect itself is the better test, and
        the probe cost one extra TCP round trip (radio wakeup) per tick."""
        if not settings.get_bool(settings.MQTT_ENABLED):
            log.info("MQTT disabled; stored locally only")
            return True
        server = settings.get(settings.MQTT_SERVER)
        if not server or not mqtt_client.paho_available():
            return True
        tls = settings.get_bool(settings.MQTT_TLS)
        port = settings.get_int(settings.MQTT_PORT, 8883 if tls else 1883)
        client = mqtt_client.FmdMqttClient(
            server, port, tls,
            settings.get(settings.MQTT_USERNAME),
            settings.get(settings.MQTT_PASSWORD),
            mqtt_client.client_id(own_id, mqtt_client.ROLE_PUB))
        ok = False
        try:
            connected = client.connect() and client.wait_connected(CONNECT_TIMEOUT_S)
            if not connected:
                # One hard retry, then give up for this tick (-> pending).
                log.warning("publisher connect failed; retrying once")
                connected = (client.force_reconnect()
                             and client.wait_connected(CONNECT_TIMEOUT_S))
            if connected:
                # Verified publish: waits for the PUBACK and internally does
                # one reconnect+republish on failure (mqtt_client._publish).
                ok = client.publish_location(own_id, payload)
            else:
                log.warning("publisher could not connect; fix not delivered")
        finally:
            # close(), not disconnect(): the per-tick client is retired here
            # and must never be revived (next tick's client reuses the same
            # client id).
            client.close()
        return ok

    def _flush_pending(self):
        """Deliver a stranded fix from an earlier tick, if any."""
        if self._pending is None:
            return
        if not settings.get_bool(settings.MQTT_ENABLED):
            self._pending = None
            return
        own_id, payload = self._pending
        if self._publish(own_id, payload):
            log.info("pending fix delivered")
            self._pending = None
        else:
            log.warning("pending fix still undeliverable; kept for next cycle")

    # -- one cycle --
    def tick(self, generation=None):
        """One GPS cycle. `generation` is the settings generation this cycle
        was started for; a save landing mid-wait cuts the cycle short so the
        loop can pick the new settings up instead of finishing a fix nobody
        asked for anymore."""
        own_id = devices.own_device_id()

        # Auto-enable location if opted in and currently off.
        if not location_control.is_enabled():
            if settings.get_bool(settings.AUTO_ENABLE_LOCATION):
                log.info("auto-enabling location services")
                location_control.set_location_enabled(enable=True)
                location_control.wait_until_enabled()  # priv service applies it async
                time.sleep(3)   # allow the provider to start before first fix
            else:
                log.info("location disabled and auto-enable off; skipping fix")
                self._flush_pending()
                return

        import gps_reader  # imported here so an early import error doesn't kill boot
        # should_abort: leave the up-to-90s fix wait as soon as SIGTERM arrives,
        # otherwise systemd's stop timeout (90s) SIGKILLs us -> unit state
        # "failed". A settings save aborts it too, so the UI does not sit on
        # "applying" for the rest of the timeout.
        abort = (_stop.is_set if generation is None
                 else _abort_on_settings_change(generation))
        fix = gps_reader.get_fix(timeout=90, should_abort=abort)
        if _stop.is_set():
            return
        # A successful fix is kept even if the settings changed meanwhile --
        # storing a good position is never wrong and costs one insert.
        if not fix.success and generation is not None \
                and runtime.generation() != generation:
            log.info("fix wait aborted by a settings change; restarting cycle")
            return
        battery = gps_reader.read_battery_level()
        if not fix.success:
            log.warning("no GPS fix: %s", fix.error)
            # No new position this tick: an older stranded one is still the
            # newest we have -- deliver it now.
            self._flush_pending()
            return
        gpsstore.store_fix(own_id, fix.timestamp_utc, fix.timestamp_local,
                           fix.lat, fix.lon, fix.alt, fix.speed,
                           fix.accuracy_h, battery)
        payload = {
            "device_id": own_id,
            "timestamp_utc": fix.timestamp_utc,
            "timestamp_local": fix.timestamp_local,
            "lat": fix.lat, "lon": fix.lon, "alt": fix.alt,
            "speed": fix.speed, "accuracy": fix.accuracy_h, "battery": battery,
        }
        if self._pending is not None:
            # retain=True: only the newest position matters.
            log.info("pending fix superseded by a newer one")
            self._pending = None
        if not self._publish(own_id, payload):
            log.warning("fix not delivered; kept pending for the next cycle")
            self._pending = (own_id, payload)

    def run(self):
        db.init_schema()
        devices.ensure_own_device()
        log.info("GPS daemon started (version %s)", DAEMON_VERSION or "?")
        beat = _Heartbeat()
        beat.start()
        while not _stop.is_set():
            try:
                # Read the generation *before* the settings: a save landing
                # between the two must not be reported as already applied.
                gen = runtime.generation()
                if settings.get_bool(settings.BACKGROUND_ENABLED):
                    beat.set("active", gen)
                    self.tick(gen)
                    interval = max(1, settings.get_int(settings.GPS_INTERVAL_MIN, 15))
                    self._sleep(interval * 60, generation=gen)
                else:
                    log.debug("background activity off; idling")
                    beat.set("idle", gen)
                    self._sleep(IDLE_POLL_SECONDS, generation=gen)
            except Exception:
                log.exception("error in GPS daemon loop; continuing")
                self._sleep(IDLE_POLL_SECONDS)
        log.info("GPS daemon stopped")

    @staticmethod
    def _sleep(seconds, generation=None):
        """Event-based nap; wakes early on shutdown or a settings change.

        Chunked instead of a single event.wait() so a settings save takes
        effect without a unit restart -- one generation check per chunk, not
        the former flag poll once per second. The heartbeat is deliberately
        NOT written here: _Heartbeat keeps it fresh on its own thread, so it
        stays alive through the long blocking parts of a tick too.
        SIGTERM interrupts immediately."""
        gen = runtime.generation() if generation is None else generation
        deadline = time.time() + seconds
        while not _stop.is_set():
            # Checked before the first wait, not after it: tick() may have just
            # been aborted by a save, and napping a full chunk first would put
            # the UI back on "applying" for another 30s.
            if runtime.generation() != gen:
                log.info("settings generation changed; leaving sleep early")
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            _stop.wait(min(remaining, runtime.HEARTBEAT_INTERVAL_S))


def main():
    global DAEMON_VERSION
    _fh = logging.handlers.RotatingFileHandler(
        "/tmp/fmd-gps.log", maxBytes=1_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FMD_LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), _fh])
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    DAEMON_VERSION = runtime.read_version_file(VERSION_FILE)
    GpsDaemon().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
