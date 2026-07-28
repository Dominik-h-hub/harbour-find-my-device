#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daemon_cmd.py -- Remote command listener service (systemd USER service).

Listens on two channels and executes remote-control commands:

  * MQTT  -- subscribes fmd/<own>/cmd, verifies the command-bound HMAC token
             (secret = own PIN), executes, and replies on fmd/<own>/cmd/ack.
  * SMS   -- via ofono (sms_command_listener); whitelist + TOTP/backup code.

Commands: RING, LOCK, GPS, CAMERA, DELETE. Each is gated by its feature toggle
in Settings; a disabled feature yields result "disabled". Every executed action
posts a local lock-screen notification (spec).

The daemon is never started/stopped from outside. It runs permanently and
steers itself from the shared SQLite DB (fmd/runtime.py):

  * If no remote feature is enabled it IDLES: all connections down, short poll
    timer, heartbeat state "idle".
  * When active, a watchdog timer keeps the heartbeat fresh and rebuilds the
    listeners when the UI bumped the settings generation counter.
  * It also forwards the UI's queued "enable system location" requests to the
    root priv service (the sandboxed UI must not touch the spool itself).

ExecStart: python3 /usr/share/harbour-find-my-device-daemon/daemon_cmd.py
"""

import logging
import logging.handlers
import os
import queue
import signal
import subprocess
import sys
import threading
import time

# Make sibling modules + the fmd package importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fmd import db, devices, gpsstore, runtime, settings, tokens
import location_control
import mqtt_client
import notify

log = logging.getLogger("fmd.daemon.cmd")

# Map each command to the settings switch that enables it.
_FEATURE_KEY = {
    "RING": settings.RING_ENABLED,
    "STOP_RING": settings.RING_ENABLED,  # stopping a ring uses the same toggle
    "LOCK": settings.LOCK_ENABLED,
    "DELETE": settings.DELETE_ENABLED,
    "CAMERA": settings.CAMERA_ENABLED,
    "GPS": settings.SMS_GPS_ENABLED
}

# If ANY of these is on, the daemon listens (MQTT and/or SMS channel);
# otherwise it idles. Must match the UI's expectation (api.get_daemon_status).
_ACTIVE_KEYS = (
    settings.RING_ENABLED, settings.LOCK_ENABLED, settings.DELETE_ENABLED,
    settings.CAMERA_ENABLED, settings.SMS_REMOTE_ENABLED, settings.SMS_GPS_ENABLED,
)

IDLE_POLL_SECONDS = 15        # how often to re-check toggles while idle
WATCHDOG_SECONDS = 5          # active-phase housekeeping timer

# Version of the installed daemon package; written into every heartbeat so the
# UI can offer an update when the bundled RPM is newer.
VERSION_FILE = "/usr/share/harbour-find-my-device-daemon/VERSION"
DAEMON_VERSION = ""

# Set by SIGTERM/SIGINT; waits block on this event instead of polling a flag
# once per second. The GLib loop reference is kept so the signal can also quit
# a running active phase immediately.
_stop = threading.Event()
_running = {"loop": None}


def _handle_signal(signum, _frame):
    log.info("signal %s received, shutting down", signum)
    _stop.set()
    loop = _running.get("loop")
    if loop is not None:
        loop.quit()


def _features_active():
    return any(settings.get_bool(k) for k in _ACTIVE_KEYS)


def _process_location_requests():
    """Forward a queued UI 'enable system location' wish to the priv service."""
    try:
        if runtime.consume_location_enable_request():
            log.info("processing UI location enable request")
            location_control.set_location_enabled(enable=True)
    except Exception:
        log.exception("location enable request failed")


# ===========================================================================
class CommandExecutor(object):
    """Executes commands and reports results. Shared by the MQTT and SMS paths."""

    def __init__(self, own_id=None):
        self._mqtt = None
        self._own_id = own_id

    def set_mqtt(self, client):
        self._mqtt = client

    # -- helpers --
    def own_id(self):
        if self._own_id is None:
            self._own_id = devices.own_device_id()
        return self._own_id

    def own_pin(self):
        own = devices.get_own()
        return own.get("pin") if own else None

    def feature_enabled(self, cmd):
        key = _FEATURE_KEY.get(cmd)
        return bool(key and settings.get_bool(key))

    def publish_ack(self, cmd, result):
        """Publish {cmd, result} on fmd/<own>/cmd/ack (also for local/SMS actions).

        Verified publish (waits for the PUBACK, self-repairs a dead
        connection) -- must only run on the command worker thread, never on
        the paho network thread or the GLib main loop. No
        is_really_connected() pre-check: a stale flag would silently drop
        acks, while publish_ack() repairs the connection itself."""
        if self._mqtt is not None:
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
            if cmd == "STOP_RING":
                return self._do_stop_ring()
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

    def _do_stop_ring(self):
        import ring_control
        ring_control.stop_current()
        notify.notify("Find My Device", "RING stopped remotely")
        return "ok"

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
            # should_abort: don't block shutdown for up to 90s, or systemd's stop
            # timeout SIGKILLs the daemon and leaves the unit in "failed".
            fix = gps_reader.get_fix(timeout=90, should_abort=_stop.is_set)
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

        msg = "GPS location requested remotely"
        if channel == "sms" and sender:
            sent = self._reply_gps_sms(sender, lat, lon, fix_dt,
                                       None if fix_dt else self._db_time(own_id))
            # Sent SMS do not show in the Messages history (raw ofono bypasses
            # commhistory), so record the reply in the notification instead.
            msg += ("\nReply sent by SMS to %s" % sender if sent
                    else "\nSMS reply to %s failed" % sender)

        notify.notify("Find My Device", msg)
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
        """Wipe user data, then reboot via the root priv service (queued request)."""
        notify.notify("Find My Device", "REMOTE WIPE started")
        log.warning("DELETE: wiping user data now")
        home = os.path.realpath(os.path.expanduser("~"))
        if not home.startswith("/home/"):
            log.error("DELETE aborted: unexpected home directory %r", home)
            return "error"
        try:
            subprocess.call(["find", home, "-mindepth", "1", "-delete"])
        except Exception as exc:
            log.error("wipe command error: %s", exc)
        log.warning("DELETE: requesting reboot via priv service")
        try:
            import priv_client
            priv_client.reboot()
        except Exception as exc:
            log.error("reboot request failed: %s", exc)
        return "ok"

    # -- gps helpers --
    def _publish_location(self, own_id, fix, battery):
        # No is_really_connected() pre-check: publish_location() repairs a
        # dead/stale connection itself; gating on the wrapper flag dropped
        # the fix whenever the flag had gone stale.
        if self._mqtt is None:
            log.warning("MQTT client not running; GPS fix not published")
            return
        self._mqtt.publish_location(own_id, {
            "device_id": own_id,
            "timestamp_utc": fix.timestamp_utc,
            "timestamp_local": fix.timestamp_local,
            "lat": fix.lat, "lon": fix.lon, "alt": fix.alt,
            "speed": fix.speed, "accuracy": fix.accuracy_h, "battery": battery,
        })

    def _reply_gps_sms(self, sender, lat, lon, fix_dt, db_time):
        """Send the GPS reply SMS. Returns True if it was sent/queued."""
        if lat is None or lon is None:
            log.warning("no coordinates available for GPS SMS reply")
            return False
        import sms_sender
        res = sms_sender.send_gps_sms(sender, lat, lon, fix_datetime=fix_dt,
                                      db_timestamp_local=db_time)
        return bool(res and res.success)

    @staticmethod
    def _db_time(own_id):
        last = gpsstore.get_latest(own_id)
        return last["timestamp_local"] if last else None


# ===========================================================================
class _CommandWorker(object):
    """Executes queued commands on one dedicated thread, in arrival order.

    Both command channels deliver on threads that must not block: MQTT
    commands arrive on the paho network thread (a verified publish_ack there
    waits for a PUBACK that exact thread would have to process -> guaranteed
    5s timeout + forced reconnect per command), SMS commands on the GLib main
    loop (a GPS fix can block up to 90s, stalling watchdog, heartbeat and
    D-Bus signal delivery). The callbacks therefore only enqueue; execution
    and the verified ack publish happen here. The queue is unbounded --
    command rate is human-scale."""

    _SHUTDOWN = object()

    def __init__(self, executor):
        self._executor = executor
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="fmd-cmd-worker")
        self._thread.start()

    def submit(self, cmd, arg, channel, sender=None):
        """Queue one command: execute, then publish the ack."""
        self._queue.put(("execute", cmd, arg, channel, sender))

    def submit_ack(self, cmd, result):
        """Queue a bare ack without executing (e.g. auth_failed)."""
        self._queue.put(("ack", cmd, result, None, None))

    def stop(self, timeout=5):
        """Signal shutdown and wait briefly. A worker stuck in a long job is
        abandoned (daemon thread); it drains the queue up to the sentinel and
        exits on its own. The phase teardown nulls the executor's MQTT client
        and close()s it, so late jobs only log their ack."""
        self._queue.put(self._SHUTDOWN)
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("command worker still busy; abandoned")

    def _run(self):
        while True:
            job = self._queue.get()
            if job is self._SHUTDOWN:
                return
            kind, cmd, a, b, sender = job
            try:
                if kind == "ack":
                    self._executor.publish_ack(cmd, a)
                else:
                    result = self._executor.execute(cmd, a, channel=b,
                                                    sender=sender)
                    self._executor.publish_ack(cmd, result)
            except Exception:
                log.exception("command worker job failed (%s %s)", kind, cmd)


# ===========================================================================
class _ConnmanWatcher(object):
    """Force a cmd-client reconnect when ConnMan's connectivity state changes.

    The cmd daemon is the only process with a real standing connection and no
    natural wakeup point; without this it notices a WLAN<->mobile handover
    only via SO_KEEPALIVE (up to ~90s idle + probe time) -- too slow for a
    command listener someone is sending RING to. Purely event-driven, zero
    extra wakeups. Best effort: if ConnMan is unreachable (emulator), the
    keepalive path still covers detection. Note the ConnMan D-Bus API is
    upstream-stable but not a guaranteed app API -- it must never be the only
    detection mechanism.
    """

    DEBOUNCE_S = 5  # a handover fires several signals in a row

    def __init__(self, get_client):
        self._get_client = get_client
        self._match = None
        self._last = 0.0

    def start(self):
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            DBusGMainLoop(set_as_default=True)  # idempotent; ofono listener needs it too
            bus = dbus.SystemBus()
            self._match = bus.add_signal_receiver(
                self._on_property_changed,
                dbus_interface="net.connman.Manager",
                signal_name="PropertyChanged",
                bus_name="net.connman",
                path="/")
            log.info("ConnMan state watcher active")
        except Exception as exc:
            log.info("ConnMan watcher unavailable (%s); relying on TCP keepalive",
                     exc)

    def stop(self):
        try:
            if self._match is not None:
                self._match.remove()
                self._match = None
        except Exception:
            pass

    def _on_property_changed(self, name, value):
        try:
            if str(name) != "State":
                return
            now = time.time()
            if now - self._last < self.DEBOUNCE_S:
                return
            self._last = now
            client = self._get_client()
            if client is None or _stop.is_set():
                return
            log.info("ConnMan state -> %s; forcing cmd mqtt reconnect", value)
            # Off the GLib main thread: force_reconnect can block ~2s tearing
            # down the old network thread.
            threading.Thread(target=client.force_reconnect, daemon=True).start()
        except Exception:
            log.exception("ConnMan state handler failed")


def _run_active_phase(own_id, generation):
    """Bring up MQTT + SMS listeners and serve until settings change or shutdown.

    Returns when the GLib loop quits: either we are stopping (signal), or the
    watchdog saw a new settings generation / all features turned off and wants
    the caller to rebuild or idle.

    `generation` is the settings generation the caller read *before* the
    settings below, echoed into the heartbeats so the UI can tell an applied
    save from one the daemon has not seen yet.
    """
    # Beat before the listeners come up: the MQTT connect below can block for
    # ~30s (two CONNACK waits), and a heartbeat older than HEARTBEAT_FRESH_S
    # makes the UI report the daemon as stopped.
    runtime.write_heartbeat("cmd", "active", DAEMON_VERSION, generation)

    executor = CommandExecutor(own_id=own_id)
    worker = _CommandWorker(executor)

    # --- MQTT command channel ---------------------------------------------
    def on_mqtt_command(device_id, payload):
        # Runs on the paho network thread: verify + enqueue only. Executing
        # or publishing the ack here would block the thread that has to
        # process the broker traffic (see _CommandWorker).
        cmd = (payload.get("cmd") or "").upper()
        arg = payload.get("arg")
        token = payload.get("token")
        pin = executor.own_pin()
        if not tokens.verify_command_token(pin, cmd, arg, token):
            log.warning("MQTT command %s: token verification FAILED", cmd)
            worker.submit_ack(cmd, "auth_failed")
            notify.notify("Find My Device", "Rejected %s (wrong PIN)" % cmd)
            return
        log.info("MQTT command %s authorized -> queued", cmd)
        worker.submit(cmd, arg, channel="mqtt")

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
        # Runs on the GLib main loop (D-Bus signal): enqueue only, a GPS fix
        # would otherwise stall watchdog/heartbeat for up to 90s.
        log.info("SMS command %s from %s -> queued", cmd, sender)
        worker.submit((cmd or "").upper(), arg, channel="sms", sender=sender)

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

    # --- network handover watcher (event-driven, no polling) --------------
    connman_watcher = _ConnmanWatcher(lambda: mqtt)
    if mqtt is not None:
        connman_watcher.start()

    # --- GLib main loop (D-Bus signal delivery) ---------------------------
    from gi.repository import GLib
    loop = GLib.MainLoop()
    _running["loop"] = loop

    state = {"last_beat": 0.0}

    def watchdog():
        """Housekeeping while serving: heartbeat, settings reload, priv wishes."""
        if _stop.is_set():
            loop.quit()
            return False
        now = time.time()
        if now - state["last_beat"] >= runtime.HEARTBEAT_INTERVAL_S:
            runtime.write_heartbeat("cmd", "active", DAEMON_VERSION, generation)
            state["last_beat"] = now
        _process_location_requests()
        if runtime.generation() != generation or not _features_active():
            log.info("settings changed; rebuilding listeners")
            loop.quit()
            return False
        return True

    runtime.write_heartbeat("cmd", "active", DAEMON_VERSION, generation)
    state["last_beat"] = time.time()
    GLib.timeout_add_seconds(WATCHDOG_SECONDS, watchdog)
    log.info("command daemon serving (MQTT %s, SMS %s)",
             "on" if mqtt else "off", "on" if sms_listener else "off")
    try:
        loop.run()
    finally:
        _running["loop"] = None
        connman_watcher.stop()
        if sms_listener:
            sms_listener.stop()
        # Stop the worker before MQTT goes down so a queued ack can still
        # get published.
        worker.stop()
        # An abandoned worker must not publish anymore: null the executor's
        # client and close() (not disconnect()) the wrapper, so a late
        # verified publish cannot revive it against the next phase's client.
        executor.set_mqtt(None)
        if mqtt:
            mqtt.close()
        log.info("command daemon listeners stopped")


def _idle_sleep(seconds, generation=None):
    """Event-based idle nap; wakes early on shutdown or settings change.

    Single event wait instead of the former once-per-second flag polling; the
    settings generation is checked once per nap (<=15s reaction time). Callers
    that already read the generation pass it in so a save landing in between is
    not slept through; None re-reads it here."""
    gen = runtime.generation() if generation is None else generation
    deadline = time.time() + seconds
    while not _stop.is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        _stop.wait(min(remaining, IDLE_POLL_SECONDS))
        if runtime.generation() != gen:
            return


def main():
    global DAEMON_VERSION
    _fh = logging.handlers.RotatingFileHandler(
        "/tmp/fmd-cmd.log", maxBytes=1_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FMD_LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), _fh])
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    DAEMON_VERSION = runtime.read_version_file(VERSION_FILE)

    db.init_schema()
    own = devices.ensure_own_device()
    own_id = own["device_id"]
    log.info("command daemon started (version %s)", DAEMON_VERSION or "?")

    while not _stop.is_set():
        try:
            # Read the generation *before* the settings below: a save landing
            # between the two must not be reported as already applied.
            gen = runtime.generation()
            # The UI's location wish must be served even while idle (used by
            # the foreground GPS flow when no remote feature is on).
            _process_location_requests()
            if _features_active():
                _run_active_phase(own_id, gen)
            else:
                runtime.write_heartbeat("cmd", "idle", DAEMON_VERSION, gen)
                _idle_sleep(IDLE_POLL_SECONDS, gen)
        except Exception:
            log.exception("error in command daemon loop; continuing")
            _idle_sleep(IDLE_POLL_SECONDS)

    log.info("command daemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
