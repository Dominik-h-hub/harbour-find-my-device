#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daemon_cmd.py -- Remote command listener service (systemd USER service).

Listens on two channels and executes remote-control commands:

  * MQTT  -- subscribes fmd/<own>/cmd, verifies the command-bound HMAC token
             (secret = own PIN), executes, and replies on fmd/<own>/cmd/ack.
  * SMS   -- via ofono (sms_command_listener); whitelist + TOTP/backup code.

Commands: RING, LOCK, GPS, CAMERA, DELETE. Each is gated by its feature toggle
in Settings; a disabled feature yields result "disabled". Every executed action
posts a local lock-screen notification (spec). paho runs in its own network thread alongside.

ExecStart: python3 /usr/share/harbour-find-my-device/qml/utilities/daemon_cmd.py
"""

import logging
import os
import signal
import subprocess
import sys

# Make sibling modules + the fmd package importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fmd import db, devices, gpsstore, settings, tokens
import mqtt_client
import notify

log = logging.getLogger("fmd.daemon.cmd")

PRIV_HELPER = "/usr/bin/harbour-find-my-device-priv-helper"

# Map each command to the settings switch that enables it.
_FEATURE_KEY = {
    "RING": settings.RING_ENABLED,
    "LOCK": settings.LOCK_ENABLED,
    "DELETE": settings.DELETE_ENABLED,
    "CAMERA": settings.CAMERA_ENABLED,
    "GPS": settings.SMS_GPS_ENABLED
}

_running = {"loop": None}


def _handle_signal(signum, _frame):
    log.info("signal %s received, shutting down", signum)
    loop = _running.get("loop")
    if loop is not None:
        loop.quit()


# ===========================================================================
class CommandExecutor(object):
    """Executes commands and reports results. Shared by the MQTT and SMS paths."""

    def __init__(self):
        self._mqtt = None

    def set_mqtt(self, client):
        self._mqtt = client

    # -- helpers --
    def own_id(self):
        return devices.own_device_id()

    def own_pin(self):
        own = devices.get_own()
        return own.get("pin") if own else None

    def feature_enabled(self, cmd):
        key = _FEATURE_KEY.get(cmd)
        return bool(key and settings.get_bool(key))

    def publish_ack(self, cmd, result):
        """Publish {cmd, result} on fmd/<own>/cmd/ack (also for local/SMS actions)."""
        if self._mqtt is not None and self._mqtt.is_connected():
            self._mqtt.publish_ack(self.own_id(), {"cmd": cmd, "result": result})
        log.info("ack: %s -> %s", cmd, result)

    # -- dispatch --
    def execute(self, cmd, arg, channel, sender=None):
        """Run one command. Returns 'ok' | 'disabled' | 'error'. Notifies owner."""
        cmd = (cmd or "").upper()
        if not self.feature_enabled(cmd):
            log.warning("command %s disabled in settings", cmd)
            return "disabled"
        try:
            if cmd == "RING":
                return self._do_ring()
            if cmd == "LOCK":
                return self._do_lock()
            if cmd == "GPS":
                return self._do_gps(channel, sender)
            if cmd == "CAMERA":
                return self._do_camera(arg)
            if cmd == "DELETE":
                return self._do_delete()
            log.warning("unknown command %s", cmd)
            return "error"
        except Exception:
            log.exception("command %s failed", cmd)
            return "error"

    # -- individual commands --
    def _do_ring(self):
        import ring_control
        ok = ring_control.ring()
        notify.notify("Find My Device", "RING activated remotely")
        return "ok" if ok else "error"

    def _do_lock(self):
        import lock_control
        result = lock_control.lock()
        notify.notify("Find My Device", "Device locked remotely (%s)" % (result or "failed"))
        return "ok" if result else "error"

    def _do_gps(self, channel, sender):
        """One-off fix: store + publish to own topic; reply by SMS on SMS trigger."""
        own_id = self.own_id()
        fix_dt = None
        lat = lon = None
        try:
            import gps_reader
            fix = gps_reader.get_fix(timeout=90)
            battery = gps_reader.read_battery_level()
        except Exception as exc:
            log.warning("gps_reader unavailable: %s", exc)
            fix = None
            battery = None

        if fix and fix.success:
            lat, lon, fix_dt = fix.lat, fix.lon, fix.timestamp_local
            gpsstore.store_fix(own_id, fix.timestamp_utc, fix.timestamp_local,
                               fix.lat, fix.lon, fix.alt, fix.speed,
                               fix.accuracy_h, battery)
            if settings.get_bool(settings.MQTT_ENABLED):
                self._publish_location(own_id, fix, battery)
        else:
            # Fall back to the last DB position (note only added to the SMS reply).
            last = gpsstore.get_latest(own_id)
            if last:
                lat, lon = last["latitude"], last["longitude"]

        if channel == "sms" and sender:
            self._reply_gps_sms(sender, lat, lon, fix_dt,
                                None if fix_dt else self._db_time(own_id))

        notify.notify("Find My Device", "GPS location requested remotely")
        return "ok" if (lat is not None) else "error"

    def _do_camera(self, arg):
        which = arg if arg in ("front", "back") else "back"
        url = settings.get(settings.WEBDAV_URL)
        user = settings.get(settings.WEBDAV_USERNAME)
        password = settings.get(settings.WEBDAV_PASSWORD)
        if not (url and user):
            log.warning("camera: WebDAV credentials not set")
            return "error"
        import camera_capture
        from fmd import paths
        own_id = self.own_id()
        fname = camera_capture.build_filename(own_id, which)
        out_path = os.path.join(paths.photos_dir(), fname)
        res = camera_capture.capture_to_file(which, out_path)
        if not res.success:
            log.error("camera capture failed: %s", res.error)
            return "error"
        dav_url = url.rstrip("/") + "/" + fname
        uploaded = camera_capture.upload_webdav(out_path, dav_url, user, password)
        notify.notify("Find My Device",
                      "Photo (%s camera) captured remotely" % which)
        return "ok" if uploaded else "error"

    def _do_delete(self):
        """Wipe user data, then reboot via the one-command root helper."""
        notify.notify("Find My Device", "REMOTE WIPE started")
        log.warning("DELETE: wiping user data now")
        home = os.path.expanduser("~")
        # Match the tested approach: delete everything under the login user's home.
        wipe_cmd = (
            'TARGET=$(getent passwd "$(loginctl list-users --no-legend | '
            "awk 'NR==1{print $1}')\" | cut -d: -f6); "
            'find "${TARGET:-%s}" -mindepth 1 -delete' % home)
        try:
            subprocess.call(["sh", "-c", wipe_cmd])
        except Exception as exc:
            log.error("wipe command error: %s", exc)
        log.warning("DELETE: requesting reboot via priv-helper")
        try:
            subprocess.call(["sudo", "-n", PRIV_HELPER, "reboot"])
        except Exception as exc:
            log.error("reboot helper failed: %s", exc)
        return "ok"

    # -- gps helpers --
    def _publish_location(self, own_id, fix, battery):
        if self._mqtt is None or not self._mqtt.is_connected():
            log.warning("MQTT not connected; GPS fix not published")
            return
        self._mqtt.publish_location(own_id, {
            "device_id": own_id,
            "timestamp_utc": fix.timestamp_utc,
            "timestamp_local": fix.timestamp_local,
            "lat": fix.lat, "lon": fix.lon, "alt": fix.alt,
            "speed": fix.speed, "accuracy": fix.accuracy_h, "battery": battery,
        })

    def _reply_gps_sms(self, sender, lat, lon, fix_dt, db_time):
        if lat is None or lon is None:
            log.warning("no coordinates available for GPS SMS reply")
            return
        import sms_sender
        sms_sender.send_gps_sms(sender, lat, lon, fix_datetime=fix_dt,
                                db_timestamp_local=db_time)

    @staticmethod
    def _db_time(own_id):
        last = gpsstore.get_latest(own_id)
        return last["timestamp_local"] if last else None


# ===========================================================================
def main():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FMD_LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    db.init_schema()
    own = devices.ensure_own_device()
    own_id = own["device_id"]
    executor = CommandExecutor()

    # --- MQTT command channel ---------------------------------------------
    def on_mqtt_command(device_id, payload):
        cmd = (payload.get("cmd") or "").upper()
        arg = payload.get("arg")
        token = payload.get("token")
        pin = executor.own_pin()
        if not tokens.verify_command_token(pin, cmd, arg, token):
            log.warning("MQTT command %s: token verification FAILED", cmd)
            executor.publish_ack(cmd, "auth_failed")
            notify.notify("Find My Device", "Rejected %s (wrong PIN)" % cmd)
            return
        log.info("MQTT command %s authorized -> executing", cmd)
        result = executor.execute(cmd, arg, channel="mqtt")
        executor.publish_ack(cmd, result)

    mqtt = None
    if settings.get_bool(settings.MQTT_ENABLED) and mqtt_client.paho_available() \
            and settings.get(settings.MQTT_SERVER):
        tls = settings.get_bool(settings.MQTT_TLS)
        port = settings.get_int(settings.MQTT_PORT, 8883 if tls else 1883)
        mqtt = mqtt_client.FmdMqttClient(
            settings.get(settings.MQTT_SERVER), port, tls,
            settings.get(settings.MQTT_USERNAME),
            settings.get(settings.MQTT_PASSWORD),
            mqtt_client.client_id(own_id, mqtt_client.ROLE_CMD),
            on_command=on_mqtt_command)
        if mqtt.connect():
            mqtt.subscribe_commands(own_id)
        executor.set_mqtt(mqtt)
    else:
        log.info("MQTT command channel not started (disabled or unconfigured)")

    # --- SMS command channel ----------------------------------------------
    def sms_authorize(cmd, arg, code, sender):
        cmd = (cmd or "").upper()
        if not settings.get_bool(settings.SMS_REMOTE_ENABLED):
            log.warning("SMS remote control disabled in settings")
            return False
        if not executor.feature_enabled(cmd):
            log.warning("SMS %s rejected: feature disabled (no code consumed)", cmd)
            return False
        # Consumes a backup code only on success; TOTP is non-destructive.
        return tokens.verify_sms_code(code)

    def sms_on_command(cmd, arg, sender):
        log.info("SMS command %s from %s -> executing", cmd, sender)
        result = executor.execute((cmd or "").upper(), arg, channel="sms",
                                  sender=sender)
        executor.publish_ack((cmd or "").upper(), result)

    sms_listener = None
    try:
        from sms_command_listener import SmsCommandListener
        whitelist = [s.strip() for s in
                     (settings.get(settings.SMS_WHITELIST) or "").splitlines()
                     if s.strip()]
        sms_listener = SmsCommandListener(whitelist, sms_authorize, sms_on_command)
        sms_listener.start()
        log.info("SMS command channel started (whitelist size %d)", len(whitelist))
    except Exception as exc:
        log.warning("SMS channel not started: %s", exc)

    # --- GLib main loop (D-Bus signal delivery) ---------------------------
    from gi.repository import GLib
    loop = GLib.MainLoop()
    _running["loop"] = loop
    log.info("command daemon running")
    try:
        loop.run()
    finally:
        if sms_listener:
            sms_listener.stop()
        if mqtt:
            mqtt.disconnect()
        log.info("command daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
