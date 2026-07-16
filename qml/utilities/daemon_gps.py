#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daemon_gps.py -- GPS background service (systemd USER service).

Periodically obtains the own-device position, stores the latest fix in SQLite and
(when enabled and online) publishes it to fmd/<device-id> over MQTT.

Runs in the user session, where geoclue lives (see GPS_NOTES.md), so it can read
a fix directly. It starts at boot but IDLES while the "Background activity"
switch is off, re-reading the toggles from the DB each tick (per the spec, both
daemons read their switches from the SQLite DB/config and idle when a feature is
off).

ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/daemon_gps.py
"""

import logging
import os
import signal
import sys
import time

# Make sibling modules + the fmd package importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fmd import db, devices, gpsstore, settings
import location_control
import mqtt_client

log = logging.getLogger("fmd.daemon.gps")

IDLE_POLL_SECONDS = 30        # how often to re-check toggles while idle
_running = {"go": True}


def _handle_signal(signum, _frame):
    log.info("signal %s received, shutting down", signum)
    _running["go"] = False


class GpsDaemon(object):
    def __init__(self):
        self._mqtt = None
        self._mqtt_key = None      # (server, port, tls, user) used to detect changes

    # -- mqtt --
    def _ensure_mqtt(self):
        """Connect/reconnect the publisher client if MQTT settings changed."""
        if not settings.get_bool(settings.MQTT_ENABLED):
            return None
        server = settings.get(settings.MQTT_SERVER)
        if not server or not mqtt_client.paho_available():
            return None
        tls = settings.get_bool(settings.MQTT_TLS)
        port = settings.get_int(settings.MQTT_PORT, 8883 if tls else 1883)
        user = settings.get(settings.MQTT_USERNAME)
        key = (server, port, tls, user)
        if self._mqtt is not None and key == self._mqtt_key:
            return self._mqtt
        if self._mqtt is not None:
            self._mqtt.disconnect()
        own_id = devices.own_device_id()
        self._mqtt = mqtt_client.FmdMqttClient(
            server, port, tls, user, settings.get(settings.MQTT_PASSWORD),
            mqtt_client.client_id(own_id, mqtt_client.ROLE_PUB))
        self._mqtt.connect()
        self._mqtt_key = key
        return self._mqtt

    def _publish(self, own_id, fix, battery):
        if not settings.get_bool(settings.MQTT_ENABLED):
            log.info("MQTT disabled; stored locally only")
            return
        server = settings.get(settings.MQTT_SERVER)
        if not mqtt_client.network_up(server or None,
                                      settings.get_int(settings.MQTT_PORT, 8883)):
            log.warning("network offline; skipping publish (DB still updated)")
            return
        client = self._ensure_mqtt()
        if client is None or not client.is_connected():
            log.warning("MQTT not connected; skipping publish this tick")
            return
        payload = {
            "device_id": own_id,
            "timestamp_utc": fix.timestamp_utc,
            "timestamp_local": fix.timestamp_local,
            "lat": fix.lat, "lon": fix.lon, "alt": fix.alt,
            "speed": fix.speed, "accuracy": fix.accuracy_h, "battery": battery,
        }
        client.publish_location(own_id, payload)

    # -- one cycle --
    def tick(self):
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
                return

        import gps_reader  # imported here so an early import error doesn't kill boot
        fix = gps_reader.get_fix(timeout=90)
        battery = gps_reader.read_battery_level()
        if not fix.success:
            log.warning("no GPS fix: %s", fix.error)
            return
        gpsstore.store_fix(own_id, fix.timestamp_utc, fix.timestamp_local,
                           fix.lat, fix.lon, fix.alt, fix.speed,
                           fix.accuracy_h, battery)
        self._publish(own_id, fix, battery)

    def run(self):
        db.init_schema()
        devices.ensure_own_device()
        log.info("GPS daemon started")
        while _running["go"]:
            try:
                if settings.get_bool(settings.BACKGROUND_ENABLED):
                    self.tick()
                    interval = max(1, settings.get_int(settings.GPS_INTERVAL_MIN, 15))
                    self._sleep(interval * 60)
                else:
                    log.debug("background activity off; idling")
                    self._sleep(IDLE_POLL_SECONDS)
            except Exception:
                log.exception("error in GPS daemon loop; continuing")
                self._sleep(IDLE_POLL_SECONDS)
        if self._mqtt:
            self._mqtt.disconnect()
        log.info("GPS daemon stopped")

    @staticmethod
    def _sleep(seconds):
        """Sleep in 1s steps so shutdown / toggle changes are picked up promptly."""
        end = time.time() + seconds
        while _running["go"] and time.time() < end:
            time.sleep(1)


def main():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FMD_LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    GpsDaemon().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
