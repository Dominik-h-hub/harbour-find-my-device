#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py -- PyOtherSide bridge for harbour-find-my-device.

Signals sent to QML (data[0] = event name):
  ('log', text)
  ('mapUpdated',)
  ('devicesUpdated',)
  ('commandResult', device_id, cmd, result)
  ('locationFix', success, message)
"""

import logging
import threading
import time

from fmd import db, devices, gpsstore, settings, tokens
import location_control
import mqtt_client

try:
    import pyotherside
    _HAVE_PYOTHERSIDE = True
except Exception:
    pyotherside = None
    _HAVE_PYOTHERSIDE = False

log = logging.getLogger("fmd.api")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# OSM zero-config default User-Agent.
OSM_USER_AGENT = "harbour-find-my-device/1.0 (contact: dominikh@atomicmail.io)"

_ui_mqtt = None
_ui_lock = threading.Lock()

# Serialises the post-save side effects (MQTT reconnect + daemon resync) which run
# in a background thread so a settings save never blocks the PyOtherSide worker.
_settings_apply_lock = threading.Lock()

# If a command was sent and no response after XX seconds, command buttons will be disabled.
_ACK_TIMEOUT_S = 60
_pending_acks = {}        # device_id -> (token, threading.Timer)
_timed_out = set()        # device ids whose last command got no ack in time
_pending_lock = threading.Lock()
_ack_seq = 0

# Devices currently ringing (optimistic UI state so the RING button can become a
# STOP button). A ring lasts ~RING_SECONDS on the target; we auto-clear a little
# later so the button reverts even if the "ring ended" is not signalled. Pressing
# STOP sends a STOP_RING command and clears the state immediately.
_RING_AUTOCLEAR_S = 65
_ringing = set()          # device ids shown as ringing
_ring_timers = {}         # device_id -> (token, threading.Timer)

# Foreground GPS polling. When "Background activity" is OFF the GPS daemon does
# not run, so the running app itself keeps taking/publishing fixes on the GPS
# interval until the user closes it. Lives only in the UI process (dies with it).
_fg_gps_timer = None      # threading.Timer for the next foreground tick
_fg_gps_lock = threading.Lock()
# Serialises GPS fixes so a foreground tick and a manual "Update map" never drive
# geoclue concurrently.
_fix_lock = threading.Lock()


# --- signal helper ---------------------------------------------------------
def _emit(event, *args):
    if _HAVE_PYOTHERSIDE:
        pyotherside.send(event, *args)
    else:
        log.debug("emit(off-device): %s %s", event, args)


def _log_ui(text):
    log.info(text)
    _emit("log", text)


# =========================================================================
# Lifecycle
# =========================================================================
def init_app():
    """First-run setup: create the DB/schema, ensure the own device exists,
    start the UI MQTT listener.
    """
    db.init_schema()
    own = devices.ensure_own_device()
    _log_ui("app initialized (own device %s)" % own["device_id"])
    _start_ui_mqtt()
    _sync_daemons()
    return {
        "own_device_id": own["device_id"],
        "own_label": devices.display_label(own),
        "gps_enabled": location_control.is_enabled(),
        "tile_provider": settings.get(settings.TILE_PROVIDER),
        "geoapify_key": settings.get(settings.GEOAPIFY_KEY),
        "osm_user_agent": OSM_USER_AGENT,
        "app_version": app_version(),
    }

#App-Version fallback if its not available via .spec file
_APP_VERSION_FALLBACK = "0.1"


def app_version():
    #Version string for the Settings 'App Version' row from .spec file.
    try:
        import subprocess
        out = subprocess.check_output(
            ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", "harbour-find-my-device"],
            stderr=subprocess.DEVNULL).decode().strip()
        if out and "not installed" not in out:
            return out
    except Exception:
        pass
    return _APP_VERSION_FALLBACK


def get_map_config():
    """Map tile provider config for the QML map. Read at startup and re-read
    after a settings change so switching osm<->geoapify takes effect without an
    app restart (the QML side recreates the Map so the plugin re-reads params).
    """
    return {
        "tile_provider": settings.get(settings.TILE_PROVIDER),
        "geoapify_key": settings.get(settings.GEOAPIFY_KEY),
        "osm_user_agent": OSM_USER_AGENT,
    }


# =========================================================================
# Settings
# =========================================================================
def get_settings():
    """Return all settings plus own-device meta for the Settings page."""
    data = settings.get_all_public()
    own = devices.ensure_own_device()
    data["own_device_id"] = own["device_id"]
    data["device_label"] = own["device_label"]
    data["own_pin_set"] = bool(own.get("pin"))
    data["pin"] = own.get("pin") or ""
    data["totp_secret"] = own.get("totp_secret") or ""
    data["totp_uri"] = (tokens.totp_uri(own["totp_secret"], own["device_label"])
                        if own.get("totp_secret") else "")
    data["backup_codes_unused"] = tokens.count_unused_backup_codes()
    data["gps_enabled"] = location_control.is_enabled()
    data["osm_user_agent"] = OSM_USER_AGENT
    return data


def save_settings(values):
    """Persist settings from the UI.
    The own-device PIN and label are routed to the devices table (not settings).
    Restarts the UI MQTT client if connection-relevant fields changed.
    """
    values = dict(values or {})
    pin = values.pop("pin", None)
    own_label = values.pop("device_label_own", None)

    # Remaining keys go straight to the settings table (known keys only).
    known = {k: v for k, v in values.items() if k in settings.DEFAULTS}
    if known:
        settings.set_many(known)

    own = devices.ensure_own_device()
    if pin is not None:
        devices.update_device(own["device_id"], pin=pin)
    if own_label is not None:
        devices.update_device(own["device_id"], label=own_label)
        settings.set(settings.DEVICE_LABEL, own_label)

    _log_ui("settings saved")
    _emit("devicesUpdated")
    # Apply side effects in a background thread so the UI save call returns at once
    threading.Thread(target=_apply_settings_side_effects, daemon=True).start()
    return True


def _apply_settings_side_effects():
    """Reconnect the UI MQTT client and resync the daemons after a settings save.
    Runs in a background thread (see save_settings)"""
    with _settings_apply_lock:
        _restart_ui_mqtt()
        _sync_daemons()


def rotate_totp_secret():
    """Generate a new TOTP secret for the own device, store it, return secret+uri."""
    secret = tokens.generate_totp_secret()
    tokens.set_own_totp_secret(secret)
    own = devices.ensure_own_device()
    _log_ui("new TOTP secret generated for own device")
    return {"secret": secret, "uri": tokens.totp_uri(secret, own["device_label"])}


def qr_matrix(text):
    """Return the QR-code module matrix for `text` (e.g. the TOTP otpauth URI).
    Pure-python, dependency-free (vendored qrcode -> get_matrix). The UI renders
    the returned rows as a grid so an authenticator app on a second device can
    scan the secret. Returns {} when there is nothing to encode.
    """
    if not text:
        return {}
    try:
        import qrcode  # vendored, pure-python (matrix only, no image backends)
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        # Compact transport: one string of '0'/'1' per row.
        rows = ["".join("1" if cell else "0" for cell in row) for row in matrix]
        return {"size": len(rows), "rows": rows}
    except Exception as exc:
        log.warning("QR generation failed: %s", exc)
        return {}


def regenerate_backup_codes():
    """Regenerate the own-device backup codes; return the plaintext list ONCE."""
    codes = tokens.generate_backup_codes()
    _log_ui("regenerated %d backup codes" % len(codes))
    return codes


# =========================================================================
# Ringtone (RING command sound)
# =========================================================================
# Sailfish standard tones.
_RINGTONE_DIRS = ("/usr/share/sounds/jolla-ringtones/stereo",)


def _tone_label(path):
    """Human-readable name from a tone filename, e.g. jolla-ringtone.ogg -> 'Jolla ringtone'."""
    import os
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("-", " ").replace("_", " ").strip().capitalize()


def list_ring_tones():
    """Return the selectable ringtones plus the currently configured one, for the
    Settings ringtone picker: {'current': path, 'tones': [{'path','name'}, ...]}."""
    import glob
    import os
    tones = []
    seen = set()
    for d in _RINGTONE_DIRS:
        for p in sorted(glob.glob(os.path.join(d, "*.ogg"))):
            tones.append({"path": p, "name": _tone_label(p)})
            seen.add(p)
    current = settings.get(settings.RING_TONE) or ""
    # Keep a custom/configured tone visible even if it lives outside the dirs.
    if current and current not in seen and os.path.isfile(current):
        tones.insert(0, {"path": current, "name": _tone_label(current)})
    return {"current": current, "tones": tones}


def preview_ring_tone(path):
    """Audition a ringtone file once (Settings preview). Plays in the UI process."""
    import ring_control
    ok = ring_control.preview(path)
    _log_ui("ringtone preview: %s -> %s" % (path, "ok" if ok else "failed"))
    return {"ok": ok}


def stop_ring_preview():
    """Stop a running ringtone preview."""
    import ring_control
    ring_control.stop_preview()
    return {"ok": True}


# =========================================================================
# Devices
# =========================================================================
def list_devices():
    """Devices for the Devices page, each with last fix and button-enable flags."""
    webdav_ok = bool(settings.get(settings.WEBDAV_URL)
                     and settings.get(settings.WEBDAV_USERNAME))
    result = []
    own_ringing = False
    try:
        import ring_control
        own_ringing = ring_control.is_ringing()
    except Exception:
        own_ringing = False
    for dev in devices.list_devices():
        fix = gpsstore.get_latest(dev["device_id"])
        has_pin = bool(dev.get("pin"))
        last_result = dev.get("last_auth_result") or ""
        auth_failed = last_result == "auth_failed"
        no_response = dev["device_id"] in _timed_out
        is_deleted = bool(dev.get("deleted"))
        # Buttons start active; grey on auth_failed, no-response (ack timeout), a
        # missing PIN (remote) or a wiped device. The own device is never greyed.
        actions_enabled = (dev["is_own"] == 1) or (
            has_pin and not auth_failed and not no_response and not is_deleted)
        result.append({
            "device_id": dev["device_id"],
            "label": devices.display_label(dev),
            "is_own": dev["is_own"],
            "has_pin": has_pin,
            "auth_failed": auth_failed,
            "no_response": no_response,
            "deleted": is_deleted,
            # The own device can be rung remotely by another device; the command
            # daemon flags that via a cross-process state file (own_ringing) so the
            # STOP button shows here too, not only on the controlling device.
            "ringing": (dev["device_id"] in _ringing)
                       or (dev["is_own"] == 1 and own_ringing),
            "last_auth_result": last_result,
            "actions_enabled": actions_enabled,
            "camera_enabled": actions_enabled and webdav_ok and dev["is_own"] == 0,
            "ring_enabled": actions_enabled,
            "last_fix": fix,
        })
    return result


def add_device(device_id, label, pin):
    ok, err = devices.add_remote(device_id, label, pin)
    if ok:
        _log_ui("device added: %s" % device_id)
        _restart_ui_mqtt()
        _emit("devicesUpdated")
    return {"ok": ok, "error": err or ""}


def get_device_pin(device_id):
    """De-obfuscated PIN for the edit form. PINs are stored reversibly obfuscated,
    so the edit page can pre-fill the saved PIN."""
    return devices.get_pin(device_id) or ""


def update_device(device_id, label, pin, new_device_id=None):
    """Update a remote device. `device_id` is the CURRENT id (lookup key);
    `new_device_id` (optional) renames it -- used by the edit form to correct a
    wrong id. Empty pin string means "leave the PIN unchanged".
    """
    new_id = (new_device_id or "").strip()
    renamed = bool(new_id and new_id != device_id)
    if renamed:
        ok, err = devices.rename_device(device_id, new_id)
        if not ok:
            return {"ok": False, "error": err or ""}
        # The transient no-ack state and the MQTT subscription are keyed by id, so
        # drop the old state and re-subscribe under the new id below.
        _clear_ack_timeout(device_id)
        device_id = new_id

    pin_arg = pin if pin else None
    ok, err = devices.update_device(device_id, label=label, pin=pin_arg)
    if ok:
        # Editing is the recovery action for a wrong id/PIN (or a re-set-up
        # device after a wipe): clear any previous auth_failed / timeout /
        # deleted state so the command buttons are usable again.
        _clear_ack_timeout(device_id)
        devices.set_auth_result(device_id, None)
        devices.set_deleted(device_id, False)
        if renamed:
            _restart_ui_mqtt()  # subscribe to the new id's location/ack topics
        _log_ui("device updated: %s" % device_id)
        _emit("devicesUpdated")
        _emit("mapUpdated")
    return {"ok": ok, "error": err or ""}


def set_own_label(label):
    """Set the own device's display label (editable from the Devices tab).
    Keeps the devices row and the DEVICE_LABEL setting in sync -- the Settings
    page reads the latter -- and refreshes the map/cover. The device-id and the
    remote-access PIN are NOT touched here (id is fixed; PIN stays in Settings).
    """
    label = (label or "").strip()
    own = devices.ensure_own_device()
    devices.update_device(own["device_id"], label=label)
    settings.set(settings.DEVICE_LABEL, label)
    _log_ui("own device label updated")
    _emit("devicesUpdated")
    _emit("mapUpdated")
    return {"ok": True}


def remove_device(device_id):
    ok, err = devices.remove_device(device_id)
    if ok:
        _clear_ack_timeout(device_id)
        _clear_ringing(device_id)
        _log_ui("device unpaired: %s" % device_id)
        _restart_ui_mqtt()
        _emit("devicesUpdated")
        _emit("mapUpdated")
    return {"ok": ok, "error": err or ""}


# =========================================================================
# Map
# =========================================================================
def get_map_data():
    """Markers + status flags for the map page."""
    server = settings.get(settings.MQTT_SERVER)
    port = settings.get_int(settings.MQTT_PORT,
                            8883 if settings.get_bool(settings.MQTT_TLS) else 1883)
    network_online = mqtt_client.network_up(server or None, port)
    gps_available = location_control.is_enabled()

    markers = []
    for row in gpsstore.get_latest_all():
        if row["latitude"] is None or row["longitude"] is None:
            continue
        markers.append({
            "device_id": row["device_id"],
            "label": row.get("device_label") or row["device_id"],
            "lat": row["latitude"],
            "lon": row["longitude"],
            "timestamp_local": row["timestamp_local"] or row["timestamp_utc"],
            "battery": row["battery_level"],
            "accuracy": row["accuracy"],
            "is_own": row.get("is_own", 0),
        })
    return {
        "devices": markers,
        "network_online": network_online,
        "gps_available": gps_available,
        "tile_provider": settings.get(settings.TILE_PROVIDER),
        "geoapify_key": settings.get(settings.GEOAPIFY_KEY),
        "osm_user_agent": OSM_USER_AGENT,
    }


def _friendly_fix_error(err):
    #Turn a raw gps_reader error into a short, user-facing message.
    e = (err or "").lower()
    if ("geoclue" in e or "serviceunknown" in e or "hybris" in e
            or "provider" in e):
        return "GPS not available on this device"
    if "timeout" in e or "timed out" in e or "no fix" in e:
        return "No GPS fix yet -- try again outdoors"
    return err or "No GPS fix"


def refresh_location():
    #ake a one-off own-device GPS fix, store it, publish it (if enabled).
    # Serialise so the foreground poll and a manual refresh can't fix at once.
    with _fix_lock:
        return _do_refresh_location(notify=True)


def _do_refresh_location(notify=True):
    # notify=False is used by the foreground poll so periodic failures (no fix /
    # GPS off) don't pop the Map banner every interval; the map/cover still update.
    own_id = devices.own_device_id()

    if not location_control.is_enabled():
        if settings.get_bool(settings.AUTO_ENABLE_LOCATION):
            _log_ui("auto-enabling location services")
            location_control.set_location_enabled(enable=True)
            time.sleep(2)  # allow the provider to start before the first fix
        else:
            if notify:
                _emit("locationFix", False, "GPS is disabled")
            _emit("mapUpdated")
            return {"ok": False, "error": "gps_disabled"}

    try:
        import gps_reader  # device-only (dbus/gi)
    except Exception as exc:
        _log_ui("gps_reader unavailable: %s" % exc)
        if notify:
            _emit("locationFix", False, "GPS reader unavailable")
        return {"ok": False, "error": "no_gps_reader"}

    fix = gps_reader.get_fix(timeout=90)
    battery = gps_reader.read_battery_level()
    if not fix.success:
        # Keep the raw error in the log; show a clean message in the UI banner.
        _log_ui("no GPS fix: %s" % fix.error)
        if notify:
            _emit("locationFix", False, _friendly_fix_error(fix.error))
        _emit("mapUpdated")
        return {"ok": False, "error": fix.error or "no_fix"}

    gpsstore.store_fix(own_id, fix.timestamp_utc, fix.timestamp_local,
                       fix.lat, fix.lon, fix.alt, fix.speed,
                       fix.accuracy_h, battery)

    _publish_own_location(own_id, fix, battery)
    if notify:
        _emit("locationFix", True, "fix stored")
    _emit("mapUpdated")
    # The own device's last fix time / battery changed -> refresh the Devices tab too.
    _emit("devicesUpdated")
    return {"ok": True}


def _publish_own_location(own_id, fix, battery):
    """Publish the own-device location to fmd/<id> if MQTT is enabled + online."""
    if not settings.get_bool(settings.MQTT_ENABLED):
        log.info("MQTT publishing disabled; stored locally only")
        return
    server = settings.get(settings.MQTT_SERVER)
    if not server:
        return
    payload = {
        "device_id": own_id,
        "timestamp_utc": fix.timestamp_utc,
        "timestamp_local": fix.timestamp_local,
        "lat": fix.lat, "lon": fix.lon, "alt": fix.alt,
        "speed": fix.speed, "accuracy": fix.accuracy_h, "battery": battery,
    }
    with _ui_lock:
        if _ui_mqtt and _ui_mqtt.is_connected():
            _ui_mqtt.publish_location(own_id, payload)
        else:
            log.warning("UI MQTT not connected; location published by daemon instead")


# =========================================================================
# Foreground GPS polling (app-side fallback when the daemon is off)
# =========================================================================
def _start_foreground_gps():
    """Begin periodic own-device fixes from the UI process. No-op if already
    running. Stops automatically when the app process exits."""
    with _fg_gps_lock:
        if _fg_gps_timer is not None:
            return
        _log_ui("foreground GPS polling started (background activity off)")
        _schedule_foreground_tick_locked()


def _stop_foreground_gps():
    global _fg_gps_timer
    with _fg_gps_lock:
        if _fg_gps_timer is None:
            return
        _fg_gps_timer.cancel()
        _fg_gps_timer = None
        _log_ui("foreground GPS polling stopped")


def _schedule_foreground_tick_locked():
    """Arm the next foreground tick. Caller must hold _fg_gps_lock."""
    global _fg_gps_timer
    minutes = settings.get_int(settings.GPS_INTERVAL_MIN, 5)
    if minutes < 1:
        minutes = 1
    _fg_gps_timer = threading.Timer(minutes * 60, _foreground_tick)
    _fg_gps_timer.daemon = True
    _fg_gps_timer.start()


def _foreground_tick():
    # If background activity got turned on meanwhile, the daemon now handles it.
    if settings.get_bool(settings.BACKGROUND_ENABLED):
        _stop_foreground_gps()
        return
    try:
        with _fix_lock:
            res = _do_refresh_location(notify=False)
        log.info("foreground GPS tick: %s", res)
    except Exception as exc:
        log.error("foreground GPS tick failed: %s", exc)
    # Reschedule unless we were stopped while fixing.
    with _fg_gps_lock:
        if _fg_gps_timer is not None:
            _schedule_foreground_tick_locked()


# =========================================================================
# Remote commands (UI -> remote device)
# =========================================================================
def send_command(device_id, cmd, arg=""):
    #Sign and publish a remote command to another device over MQTT.
    cmd = (cmd or "").upper()
    dev = devices.get_device(device_id)
    if not dev:
        return {"ok": False, "error": "unknown device"}
    pin = dev.get("pin")
    if not pin:
        return {"ok": False, "error": "no PIN set for this device"}

    token = tokens.make_command_token(pin, cmd, arg)
    payload = {"cmd": cmd, "token": token}
    if arg:
        payload["arg"] = arg

    with _ui_lock:
        if not (_ui_mqtt and _ui_mqtt.is_connected()):
            _log_ui("cannot send %s to %s: MQTT not connected" % (cmd, device_id))
            return {"ok": False, "error": "mqtt_offline"}
        _ui_mqtt.publish_command(device_id, payload)
    _log_ui("sent command %s to %s" % (cmd, device_id))
    # Only remote devices are tracked for an ack timeout: the own device's buttons
    # are never greyed, so a missing self-ack must not flag it as "no response".
    if dev.get("is_own") != 1:
        _arm_ack_timeout(device_id, cmd)
    # Toggle the optimistic ringing state so the RING button can show STOP.
    if cmd == "RING":
        _mark_ringing(device_id)
        _emit("devicesUpdated")
    elif cmd == "STOP_RING":
        _clear_ringing(device_id)
        _emit("devicesUpdated")
    return {"ok": True}


def _mark_ringing(device_id):
    """Flag a device as ringing (RING button -> STOP) until STOP_RING or auto-clear."""
    global _ack_seq
    with _pending_lock:
        old = _ring_timers.pop(device_id, None)
        if old is not None:
            old[1].cancel()
        _ack_seq += 1
        token = _ack_seq
        _ringing.add(device_id)
        timer = threading.Timer(_RING_AUTOCLEAR_S, _on_ring_autoclear,
                                args=(device_id, token))
        timer.daemon = True
        _ring_timers[device_id] = (token, timer)
        timer.start()


def _clear_ringing(device_id):
    """Clear the ringing flag and cancel its auto-clear timer."""
    with _pending_lock:
        _ringing.discard(device_id)
        entry = _ring_timers.pop(device_id, None)
    if entry is not None:
        entry[1].cancel()


def _on_ring_autoclear(device_id, token):
    """The ring has (almost certainly) ended: revert the button to RING."""
    with _pending_lock:
        entry = _ring_timers.get(device_id)
        if entry is None or entry[0] != token:
            return
        _ring_timers.pop(device_id, None)
        _ringing.discard(device_id)
    _emit("devicesUpdated")


def _arm_ack_timeout(device_id, cmd):
    """Start (or restart) the no-ack timer for a device's pending command."""
    global _ack_seq
    with _pending_lock:
        _timed_out.discard(device_id)
        old = _pending_acks.get(device_id)
        if old is not None:
            old[1].cancel()
        _ack_seq += 1
        token = _ack_seq
        timer = threading.Timer(_ACK_TIMEOUT_S, _on_ack_timeout,
                                args=(device_id, cmd, token))
        timer.daemon = True
        _pending_acks[device_id] = (token, timer)
        timer.start()


def _clear_ack_timeout(device_id):
    """Cancel a pending no-ack timer and clear the no-ack flag (an ack arrived, or
    the device changed)."""
    with _pending_lock:
        entry = _pending_acks.pop(device_id, None)
        _timed_out.discard(device_id)
    if entry is not None:
        entry[1].cancel()


def _on_ack_timeout(device_id, cmd, token):
    """Fired when no ack arrived in time: flag the device as not responding so the
    UI greys its command buttons and shows a status under it."""
    with _pending_lock:
        entry = _pending_acks.get(device_id)
        if entry is None or entry[0] != token:
            return  # superseded by a newer command or already acked
        _pending_acks.pop(device_id, None)
        _timed_out.add(device_id)
    _log_ui("no ack from %s for %s within %ds" % (device_id, cmd, _ACK_TIMEOUT_S))
    _emit("commandResult", device_id, cmd, "timeout")
    _emit("devicesUpdated")


# =========================================================================
# Daemon status (Settings overview)
# =========================================================================
_DAEMONS = {
    "gps": "harbour-find-my-device-daemon-gps.service",
    "cmd": "harbour-find-my-device-daemon-cmd.service",
}


# Remote-control features that require the command listener to be running.
# If ANY of these is on, the cmd daemon must run (MQTT and/or SMS channel).
_CMD_FEATURE_KEYS = (
    settings.RING_ENABLED, settings.LOCK_ENABLED, settings.DELETE_ENABLED,
    settings.CAMERA_ENABLED, settings.SMS_REMOTE_ENABLED, settings.SMS_GPS_ENABLED,
)


def _set_daemon_state(unit, wanted):
    """Make the systemd USER unit match `wanted`.

    When wanted, we `restart` (not just `start`) so a running daemon re-reads the
    freshly saved settings/DB instead of keeping its old in-memory config; when
    not wanted, we `stop` it so a disabled feature truly idles. Best-effort: any
    failure is logged, never raised.
    """
    import subprocess
    action = "restart" if wanted else "stop"
    try:
        # --no-block: queue the job in systemd and return at once instead of blocking settings reloading
        subprocess.call(["systemctl", "--user", "--no-block", action, unit],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("daemon %s -> %s (wanted=%s)", unit, action, wanted)
    except Exception as exc:
        log.warning("could not %s daemon %s: %s", action, unit, exc)


def _sync_daemons():
    #Start/stop/reload the two user daemons to match current settings.
    cmd_wanted = any(settings.get_bool(k) for k in _CMD_FEATURE_KEYS)
    gps_wanted = settings.get_bool(settings.BACKGROUND_ENABLED)
    _set_daemon_state(_DAEMONS["cmd"], cmd_wanted)
    _set_daemon_state(_DAEMONS["gps"], gps_wanted)
    # When the background daemon is off, the running app polls GPS itself so the
    # location keeps being published until the user closes the app. When the
    # daemon is on it owns the polling (also while the app is closed).
    if gps_wanted:
        _stop_foreground_gps()
    else:
        _start_foreground_gps()


def get_daemon_status():
    """Return {'gps': 'running'|'deactivated'|'failed'|'unknown', 'cmd': ...}."""
    import subprocess
    result = {}
    for key, unit in _DAEMONS.items():
        try:
            out = subprocess.check_output(
                ["systemctl", "--user", "is-active", unit],
                stderr=subprocess.STDOUT).decode().strip()
        except subprocess.CalledProcessError as exc:
            out = exc.output.decode().strip() if exc.output else "unknown"
        except Exception:
            out = "unknown"
        result[key] = {
            "active": "running",
            "inactive": "deactivated",
            "failed": "failed",
        }.get(out, out or "unknown")
    return result


def enable_location_now():
    """Manually enable the system location services (used by the GPS prompt)."""
    location_control.set_location_enabled(enable=True)
    enabled = location_control.is_enabled()
    _log_ui("location enable requested -> enabled=%s" % enabled)
    return {"ok": enabled}


# =========================================================================
# UI MQTT listener (remote-device locations + acks)
# =========================================================================
def _start_ui_mqtt():
    """(Re)start the persistent UI MQTT client based on current settings."""
    global _ui_mqtt
    if not settings.get_bool(settings.MQTT_ENABLED):
        log.info("MQTT disabled in settings; UI listener not started")
        return
    server = settings.get(settings.MQTT_SERVER)
    if not server or not mqtt_client.paho_available():
        return
    tls = settings.get_bool(settings.MQTT_TLS)
    port = settings.get_int(settings.MQTT_PORT, 8883 if tls else 1883)
    own_id = devices.own_device_id()

    with _ui_lock:
        if _ui_mqtt is not None:
            _ui_mqtt.disconnect()
        _ui_mqtt = mqtt_client.FmdMqttClient(
            server, port, tls,
            settings.get(settings.MQTT_USERNAME),
            settings.get(settings.MQTT_PASSWORD),
            mqtt_client.client_id(own_id, mqtt_client.ROLE_UI),
            on_location=_on_remote_location,
            on_ack=_on_remote_ack)
        if _ui_mqtt.connect():
            # Listen to every remote device's location + ack topics.
            for dev in devices.list_devices():
                if dev["is_own"] == 1:
                    continue
                _ui_mqtt.subscribe_location(dev["device_id"])
                _ui_mqtt.subscribe_ack(dev["device_id"])
            # Also listen to the own ack topic: the command daemon acks here when it
            # executes a locally-triggered command (e.g. a RING sent from another
            # device), which lets us refresh the own-device row's STOP button.
            _ui_mqtt.subscribe_ack(own_id)
            log.info("UI MQTT listener started")


def _restart_ui_mqtt():
    _start_ui_mqtt()


def _on_remote_location(device_id, payload):
    """Store an incoming remote-device location and refresh the map."""
    if not device_id:
        return
    try:
        gpsstore.store_fix(
            device_id,
            payload.get("timestamp_utc") or devices.iso_utc(),
            payload.get("timestamp_local"),
            payload.get("lat"), payload.get("lon"), payload.get("alt"),
            payload.get("speed"), payload.get("accuracy"), payload.get("battery"))
        _log_ui("updated location for remote device %s" % device_id)
        _emit("mapUpdated")
        # The device's last fix time / battery changed -> refresh the Devices tab.
        _emit("devicesUpdated")
    except Exception as exc:
        log.error("failed storing remote location for %s: %s", device_id, exc)


def _on_remote_ack(device_id, payload):
    """Handle an ack from a remote device: update its button state."""
    if not device_id:
        return
    # Own ack: the command daemon executed a locally-triggered command (e.g. a
    # RING from another device). The own device is never greyed, so skip the
    # remote button-state logic; just refresh so the ring state (STOP button)
    # picked up from ring_control.is_ringing() is reflected immediately.
    if device_id == devices.own_device_id():
        _log_ui("own ack: %s -> %s"
                % ((payload.get("cmd") or "?"), payload.get("result")))
        _emit("devicesUpdated")
        return
    # An ack arrived -> cancel the no-ack timeout for this device.
    _clear_ack_timeout(device_id)
    result = payload.get("result")
    cmd = payload.get("cmd", "?")
    if result in ("ok", "auth_failed", "disabled"):
        devices.set_auth_result(device_id, result)
    # A confirmed DELETE means the device wiped itself: it will never answer
    # again, so flag it permanently (until the entry is edited/re-paired).
    if (cmd or "").upper() == "DELETE" and result == "ok":
        devices.set_deleted(device_id, True)
    # If a RING was not actually accepted, drop the optimistic ringing state so the
    # button does not stay on STOP for a device that never started ringing.
    if (cmd or "").upper() == "RING" and result != "ok":
        _clear_ringing(device_id)
    _log_ui("ack from %s: %s -> %s" % (device_id, cmd, result))
    _emit("commandResult", device_id, cmd, result or "")
    _emit("devicesUpdated")
