#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py -- PyOtherSide bridge for harbour-find-my-device.

Signals sent to QML (data[0] = event name):
  ('log', text)
  ('mapUpdated',)
  ('devicesUpdated',)
  ('commandResult', device_id, cmd, result)
  ('locationFix', success, error_code)  # error_code localized in QML (MapView)
"""

import logging
import logging.handlers
import queue
import threading
import time

from fmd import db, devices, gpsstore, runtime, settings, tokens
import mqtt_client

try:
    import pyotherside
    _HAVE_PYOTHERSIDE = True
except Exception:
    pyotherside = None
    _HAVE_PYOTHERSIDE = False

log = logging.getLogger("fmd.api")
_fh = logging.handlers.RotatingFileHandler(
    "/tmp/fmd-app.log", maxBytes=1_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(), _fh])

# Contact address for the OSM tile User-Agent; the version is not hardcoded
# here, see osm_user_agent().
_OSM_CONTACT = "dominikh@atomicmail.io"

_ui_mqtt = None
_ui_lock = threading.Lock()
# Newest own-device fix that could not be published because the UI client was
# offline at tick time; flushed on the next (re)connect. Guarded by _ui_lock.
_pending_location = None

# Serialises the post-save side effects (MQTT reconnect + daemon resync) which run
# in a background thread so a settings save never blocks the PyOtherSide worker.
_settings_apply_lock = threading.Lock()

# If a command was sent and no response after XX seconds, command buttons will be disabled.
_ACK_TIMEOUT_S = 60
_pending_acks = {}        # device_id -> (token, threading.Timer)
_timed_out = set()        # device ids whose last command got no ack in time
_pending_lock = threading.Lock()
_ack_seq = 0

# Outgoing commands are published on a dedicated worker thread.
_command_queue = queue.Queue()
_command_worker = None
_command_worker_lock = threading.Lock()

# Commands that could not be delivered and are safe to retry on the next
# reconnect. Deliberately STOP_RING only: a late STOP_RING is exactly what the
# user wanted (silence), whereas a RING/LOCK/CAMERA arriving minutes after the
# tap would be a surprise action nobody is asking for any more.
_RETRY_COMMANDS = ("STOP_RING",)
_PENDING_CMD_TTL_S = 60
_pending_commands = {}    # device_id -> (cmd, payload, deadline); under _ui_lock

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
# Generation of the polling chain, bumped on every start/stop. A tick carries
# the generation it was armed under and only reschedules while that is still
# current. Without it, a tick that was still running when the chain was stopped
# and restarted (a fix blocks for up to 100s, a settings save takes seconds)
# rescheduled off the successor's timer variable and forked a SECOND, permanent
# chain -- visible as every position being published twice, ~90s apart.
_fg_gps_gen = 0
_fg_in_failure = False    # True once a foreground tick failed; reset on next fix.
                          # Used to show the "no fix" banner only on the first
                          # failure of an episode, not every interval.
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
    _apply_foreground_gps()
    return {
        "own_device_id": own["device_id"],
        "own_label": devices.display_label(own),
        "gps_enabled": _location_enabled(),
        "tile_provider": settings.get(settings.TILE_PROVIDER),
        "geoapify_key": settings.get(settings.GEOAPIFY_KEY),
        "osm_user_agent": osm_user_agent(),
        "app_version": app_version(),
    }

#App-Version fallback if the VERSION file written by the .spec is missing
_APP_VERSION_FALLBACK = "0.1"
_APP_VERSION_FILE = "/usr/share/harbour-find-my-device/VERSION"


def app_version():
    #Version string for the Settings 'App Version' row. The spec writes a
    #VERSION file into the app's own share dir (readable under Sailjail);
    #no rpm(1) call -- that is not available inside the sandbox.
    return runtime.read_version_file(_APP_VERSION_FILE) or _APP_VERSION_FALLBACK


def osm_user_agent():
    # User-Agent for the OSM tile requests.
    return "harbour-find-my-device/%s (contact: %s)" % (app_version(), _OSM_CONTACT)


def get_map_config():
    """Map tile provider config for the QML map. Read at startup and re-read
    after a settings change so switching osm<->geoapify takes effect without an
    app restart (the QML side recreates the Map so the plugin re-reads params).
    """
    return {
        "tile_provider": settings.get(settings.TILE_PROVIDER),
        "geoapify_key": settings.get(settings.GEOAPIFY_KEY),
        "osm_user_agent": osm_user_agent(),
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
    data["gps_enabled"] = _location_enabled()
    data["osm_user_agent"] = osm_user_agent()
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
    """Reconnect the UI MQTT client and signal the daemons after a settings save.
    Runs in a background thread (see save_settings). The daemons are NOT
    controlled via systemd (impossible under Sailjail): they poll the settings
    generation counter and reconfigure themselves."""
    with _settings_apply_lock:
        runtime.bump_generation()
        _start_ui_mqtt()
        _apply_foreground_gps()


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


# NOTE: the ringtone preview plays in QML (QtMultimedia Audio element in the
# Settings page). The former python preview used GStreamer via gi, which is
# not allowed in the sandboxed GUI process (Harbour rules).


def _own_ringing():
    """True if the command daemon currently rings this device.

    Reads the cross-process ring state file the daemon maintains in the shared
    data dir (see ring_control.py in the daemon package); the GUI must not
    import ring_control itself (GStreamer/gi)."""
    import os
    from fmd import paths
    state = os.path.join(paths.data_dir(), "ring_active")
    try:
        with open(state) as fh:
            until = float(fh.read().strip() or "0")
    except (OSError, ValueError):
        return False
    return time.time() < until


# =========================================================================
# Devices
# =========================================================================
def list_devices():
    """Devices for the Devices page, each with last fix and button-enable flags."""
    webdav_ok = bool(settings.get(settings.WEBDAV_URL)
                     and settings.get(settings.WEBDAV_USERNAME))
    result = []
    own_ringing = _own_ringing()
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
# Cached network state for the map banner. The actual probe runs in a background
# thread: get_map_data() is served by the single PyOtherSide worker thread, and a
# synchronous probe on a just-dropped network can hang in DNS for minutes, which
# froze the whole app (every Bridge.call queues behind it).
_NET_TTL_S = 10.0
_net_state = {"online": True, "checked": 0.0, "probing": False}
_net_lock = threading.Lock()

# Rate limit for the handover check itself. It costs a route lookup (and, on a
# cold DNS cache, a resolve), so callers can ask as often as they like.
_HANDOVER_TTL_S = 10.0
_handover_check = {"ts": 0.0}
_handover_lock = threading.Lock()


def _network_online(server, port):
    """Return the last known online state; refresh it in the background if stale."""
    now = time.time()
    with _net_lock:
        stale = (now - _net_state["checked"]) > _NET_TTL_S
        if stale and not _net_state["probing"]:
            _net_state["probing"] = True
            threading.Thread(target=_probe_network, args=(server, port),
                             daemon=True).start()
        return _net_state["online"]


def _probe_network(server, port):
    online = mqtt_client.network_up(server or None, port)
    with _net_lock:
        changed = online != _net_state["online"]
        _net_state.update(online=online, checked=time.time(), probing=False)
    # Handover check piggybacks on this existing background probe (no extra
    # timer). Already off the PyOtherSide worker, so repair synchronously.
    if online:
        _repair_if_handover(server, port)
    if changed:
        # Reload the map page so the offline banner (dis)appears promptly.
        _emit("mapUpdated")


def _repair_if_handover(server=None, port=None):
    """Restart the UI client if its socket was stranded by a handover.

    Callers must be off the PyOtherSide worker thread: this waits for the new
    CONNACK so the caller can publish immediately afterwards.

    Rate-limited, so command dispatch can call it on every send. It must:
    piggybacking the check on the map's _probe_network alone is a chicken-and-
    egg trap, because get_map_data() is driven by 'mapUpdated', which is mostly
    driven by *incoming* MQTT traffic -- precisely what stops arriving when the
    socket is stranded."""
    now = time.time()
    with _handover_lock:
        if now - _handover_check["ts"] < _HANDOVER_TTL_S:
            return False
        _handover_check["ts"] = now
    if server is None:
        tls = settings.get_bool(settings.MQTT_TLS)
        server = settings.get(settings.MQTT_SERVER)
        port = settings.get_int(settings.MQTT_PORT, 8883 if tls else 1883)
    if not _handover_detected(server, port):
        return False
    log.info("network handover detected; restarting UI MQTT client")
    with _net_lock:
        _net_state["checked"] = 0.0  # invalidate: re-probe promptly
    _restart_ui_mqtt_sync()
    with _ui_lock:
        client = _ui_mqtt
    if client is not None:
        client.wait_connected()
    return True


def _handover_detected(server, port):
    """True if the UI client's socket no longer matches the preferred route.

    Conservative: any inability to determine an address (DNS failure, no
    socket, no route) means "skip", never "handover"."""
    if not server:
        return False
    try:
        import net_watch
        with _ui_lock:
            client = _ui_mqtt
        if client is None:
            return False
        current = client.local_ip()
        if current is None:
            return False
        preferred = net_watch.preferred_src_ip(server, port)
        if preferred is None:
            return False
        return preferred != current
    except Exception:
        log.exception("handover check failed")
        return False


def get_map_data():
    """Markers + status flags for the map page."""
    server = settings.get(settings.MQTT_SERVER)
    port = settings.get_int(settings.MQTT_PORT,
                            8883 if settings.get_bool(settings.MQTT_TLS) else 1883)
    network_online = _network_online(server, port)
    gps_available = _location_enabled()

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
        "osm_user_agent": osm_user_agent(),
    }


# --- location availability (read-only, sandbox-safe) -----------------------
# The GUI must not import dbus/gi or touch the priv spool. It only READS the
# system location switch from the well-known config files; if they are not
# visible inside the sandbox, assume location is available and let the QML
# PositionSource find out (worst case: a "no fix" banner after the timeout).
_LOCATION_CONF_PATHS = ("/var/lib/location/location.conf",
                        "/etc/location/location.conf")


def _location_enabled():
    readable = False
    import re
    for path in _LOCATION_CONF_PATHS:
        try:
            with open(path, "r") as fh:
                text = fh.read()
        except OSError:
            continue
        readable = True
        if re.search(r"^enabled\s*=\s*true\s*$", text, re.MULTILINE):
            return True
    return not readable  # unreadable (sandbox) -> assume enabled


def _read_battery_level():
    """Best-effort battery percentage (0-100) or None. Same sources as the
    daemon's gps_reader, but duplicated here so the GUI needs no gi/dbus."""
    import glob
    candidates = ["/run/state/namespaces/Battery/ChargePercentage"]
    candidates += sorted(glob.glob("/sys/class/power_supply/*/capacity"))
    for p in candidates:
        try:
            with open(p) as fh:
                return int(float(fh.read().strip()))
        except Exception:
            continue
    return None


# --- QML foreground fix (QtPositioning via PositionSource) ------------------
# The sandboxed GUI cannot use gps_reader (dbus/gi). Instead it asks the QML
# side for a fix: _qml_fix() emits 'requestGpsFix', GpsSource.qml activates a
# PositionSource and reports back through qml_fix_result(). The calling worker
# thread blocks on an event until the result (or timeout) arrives.
class _QmlFix(object):
    def __init__(self, ok, error=None, lat=None, lon=None, alt=None,
                 speed=None, accuracy_h=None):
        self.success = ok
        self.error = error
        self.lat, self.lon, self.alt = lat, lon, alt
        self.speed, self.accuracy_h = speed, accuracy_h
        self.timestamp_utc = devices.iso_utc()
        self.timestamp_local = time.strftime("%Y-%m-%dT%H:%M:%S")


_qml_fix_state = {"seq": 0, "result": None}
_qml_fix_event = threading.Event()
_qml_fix_seq_lock = threading.Lock()


def _qml_fix(timeout=90):
    """Request one GPS fix from the QML PositionSource; blocks the caller."""
    if not _HAVE_PYOTHERSIDE:
        return _QmlFix(False, error="no QML runtime (off-device)")
    with _qml_fix_seq_lock:
        _qml_fix_state["seq"] += 1
        seq = _qml_fix_state["seq"]
        _qml_fix_state["result"] = None
        _qml_fix_event.clear()
    _emit("requestGpsFix", seq, int(timeout * 1000))
    if not _qml_fix_event.wait(timeout + 10):
        return _QmlFix(False, error="timeout waiting for QML fix")
    result = _qml_fix_state["result"]
    if result is None or result[0] != seq:
        return _QmlFix(False, error="superseded fix request")
    return result[1]


def qml_fix_result(seq, ok, data):
    """Called from QML (GpsSource.qml) with the outcome of a fix request.
    `data` is a dict with lat/lon/alt/speed/accuracy on success, or an error
    string on failure."""
    if ok:
        d = data or {}
        fix = _QmlFix(True, lat=d.get("lat"), lon=d.get("lon"),
                      alt=d.get("alt"), speed=d.get("speed"),
                      accuracy_h=d.get("accuracy"))
    else:
        fix = _QmlFix(False, error=str(data or "no fix"))
    _qml_fix_state["result"] = (int(seq), fix)
    _qml_fix_event.set()
    return True


def _fix_error_code(err):
    e = (err or "").lower()
    if ("geoclue" in e or "serviceunknown" in e or "hybris" in e
            or "provider" in e):
        return "gps_unavailable"
    if "timeout" in e or "timed out" in e or "no fix" in e:
        return "no_fix"
    return "no_fix"


def refresh_location():
    #Take a one-off own-device GPS fix, store it, publish it (if enabled).
    threading.Thread(target=_refresh_location_worker, daemon=True).start()
    return {"ok": True}


def _refresh_location_worker():
    try:
        # Serialise so the foreground poll and a manual refresh can't fix at once.
        with _fix_lock:
            _do_refresh_location(notify=True)
    except Exception as exc:
        log.error("refresh_location failed: %s", exc)
        # Always resolve the UI (busy indicator stops on this signal).
        _emit("locationFix", False, "error")


def _do_refresh_location(notify=True, notify_fail=None):
    # notify=False is used by the foreground poll so periodic successes don't pop
    # the Map banner every interval; the map/cover still update. notify_fail
    # controls the failure banner separately (default: same as notify) so the
    # foreground poll can announce the first failure of an episode while staying
    # silent on success -- see _foreground_tick.
    if notify_fail is None:
        notify_fail = notify
    own_id = devices.own_device_id()

    if not _location_enabled():
        if settings.get_bool(settings.AUTO_ENABLE_LOCATION):
            # The sandboxed GUI cannot flip the system switch itself; it queues
            # a wish that the (unsandboxed) cmd daemon forwards to the priv
            # service. Without an installed daemon the request stays pending
            # and the fix attempt below reports its failure via the banner.
            _log_ui("requesting location enable via background service")
            runtime.request_location_enable()
            time.sleep(3)  # give the daemon a moment to apply it
        else:
            if notify_fail:
                _emit("locationFix", False, "gps_disabled")
            _emit("mapUpdated")
            return {"ok": False, "error": "gps_disabled"}

    fix = _qml_fix(timeout=90)
    battery = _read_battery_level()
    if not fix.success:
        # Keep the raw error in the log; show a clean message in the UI banner.
        _log_ui("no GPS fix: %s" % fix.error)
        if notify_fail:
            _emit("locationFix", False, _fix_error_code(fix.error))
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
    global _pending_location
    with _ui_lock:
        client = _ui_mqtt
    # No is_really_connected() pre-check: publish_location() repairs a dead or
    # stale connection itself (reconnect + republish). Gating here on the
    # wrapper flag parked every fix on _pending_location once the flag went
    # stale, because the on_connected flush only fires on an actual reconnect.
    # Published outside _ui_lock: the repair can block a few seconds and the
    # flush callback (paho network thread) needs the lock meanwhile.
    if client is not None and client.publish_location(own_id, payload):
        with _ui_lock:
            _pending_location = None
        return
    # With background activity off no daemon is running, so nobody else would
    # publish this fix -- keep the newest one and flush it on reconnect.
    with _ui_lock:
        _pending_location = (own_id, payload)
    log.warning("UI MQTT publish failed or client not running; fix stored "
                "locally, will publish on reconnect")


# =========================================================================
# Foreground GPS polling (app-side fallback when the daemon is off)
# =========================================================================
def _start_foreground_gps():
    """Begin periodic own-device fixes from the UI process. No-op if already
    running. Stops automatically when the app process exits."""
    global _fg_gps_gen
    with _fg_gps_lock:
        if _fg_gps_timer is not None:
            return
        _fg_gps_gen += 1
        _log_ui("foreground GPS polling started (background activity off)")
        # Quick first tick: auto-enable location and the GPS cold start should
        # begin right away, not one full interval after the app started.
        _schedule_foreground_tick_locked(delay_s=15)


def _stop_foreground_gps():
    global _fg_gps_timer, _fg_in_failure, _fg_gps_gen
    with _fg_gps_lock:
        if _fg_gps_timer is None:
            return
        _fg_gps_timer.cancel()
        _fg_gps_timer = None
        # Retires any tick that is still mid-fix: cancel() cannot reach one that
        # has already fired, and on return it would otherwise reschedule itself.
        _fg_gps_gen += 1
        _log_ui("foreground GPS polling stopped")
    # Handing off to the background daemon: a stale foreground "no fix" banner no
    # longer applies (the daemon runs in its own process and cannot drive the UI
    # banner), so clear it and reset the failure state for the next foreground run.
    if _fg_in_failure:
        _fg_in_failure = False
        _emit("locationFix", True, "")


def _schedule_foreground_tick_locked(delay_s=None):
    """Arm the next foreground tick (after the configured interval, or after
    delay_s seconds if given). Caller must hold _fg_gps_lock."""
    global _fg_gps_timer
    if delay_s is None:
        minutes = settings.get_int(settings.GPS_INTERVAL_MIN, 5)
        if minutes < 1:
            minutes = 1
        delay_s = minutes * 60
    _fg_gps_timer = threading.Timer(delay_s, _foreground_tick,
                                    args=(_fg_gps_gen,))
    _fg_gps_timer.daemon = True
    _fg_gps_timer.start()


def _foreground_tick(gen):
    global _fg_in_failure
    # Drop a tick whose chain was retired while it sat in the timer.
    with _fg_gps_lock:
        if gen != _fg_gps_gen:
            log.info("foreground GPS tick from retired chain %d (now %d); dropped",
                     gen, _fg_gps_gen)
            return
    # If the background daemon took over meanwhile, it now handles the polling.
    # (Only the flag being set is not enough: without an installed daemon the
    # app keeps polling in the foreground -- degraded mode.)
    if _background_daemon_owns_gps():
        _stop_foreground_gps()
        return
    try:
        log.info("foreground GPS tick: fetching fix")
        # Announce a failure only on the first tick of a failure episode, not on
        # every interval; a successful fix re-arms the banner for the next one.
        with _fix_lock:
            res = _do_refresh_location(notify=False,
                                       notify_fail=not _fg_in_failure)
        ok = res.get("ok", False)
        # Recovered from a failure episode: clear the persistent "no fix" banner
        # (foreground successes are otherwise silent, so nothing else clears it).
        if ok and _fg_in_failure:
            _emit("locationFix", True, "")
        _fg_in_failure = not ok
        log.info("foreground GPS tick: %s", res)
    except Exception as exc:
        log.error("foreground GPS tick failed: %s", exc)
    # Reschedule only if this chain is still the current one. Testing
    # _fg_gps_timer alone is not enough: a stop+start during the fix (a settings
    # save while a tick blocks for up to 100s) leaves it pointing at the
    # SUCCESSOR's timer, so this tick would arm a second, parallel chain and
    # every position would be published twice from then on.
    with _fg_gps_lock:
        if _fg_gps_timer is not None and gen == _fg_gps_gen:
            _schedule_foreground_tick_locked()


# =========================================================================
# Remote commands (UI -> remote device)
# =========================================================================
def send_command(device_id, cmd, arg=""):
    """Sign a remote command and queue it for the command worker.

    Returns as soon as the command is queued -- the publish itself must never
    run on the PyOtherSide worker thread. A verified publish blocks until the
    PUBACK and spends up to ~40s repairing a dying link, and every Bridge.call
    is served by that one thread, so publishing here froze the whole backend.
    That is what made the channel look "blocked" after a RING: the STOP_RING
    tap behind it was not refused, it was simply not processed yet.

    Delivery is reported asynchronously via the 'commandResult' signal
    ('sent' / 'pending' / 'mqtt_offline'), the target's answer via the ack.
    """
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

    # Toggle the optimistic ringing state so the RING button can show STOP. Done
    # on the tap, not after delivery, so the button follows the finger; the
    # dispatcher reverts it if the RING never made it out.
    if cmd == "RING":
        _mark_ringing(device_id)
        _emit("devicesUpdated")
    elif cmd == "STOP_RING":
        _clear_ringing(device_id)
        _emit("devicesUpdated")

    _ensure_command_worker()
    _command_queue.put((device_id, cmd, payload, dev.get("is_own") == 1))
    return {"ok": True, "queued": True}


def _ensure_command_worker():
    """Start the command worker on first use (and revive it if it ever died)."""
    global _command_worker
    with _command_worker_lock:
        if _command_worker is not None and _command_worker.is_alive():
            return
        _command_worker = threading.Thread(target=_command_worker_run,
                                           name="fmd-cmd-dispatch", daemon=True)
        _command_worker.start()


def _command_worker_run():
    """Publish queued commands one at a time, in tap order."""
    while True:
        job = _command_queue.get()
        try:
            _dispatch_command(*job)
        except Exception:
            log.exception("command dispatch failed")


def _dispatch_command(device_id, cmd, payload, is_own):
    """Publish one queued command. Runs on the command worker thread."""
    # Repair a stranded socket BEFORE the publish rather than through it: the
    # in-publish repair pays a 5s PUBACK timeout first, and after a handover
    # that first attempt is doomed anyway.
    _repair_if_handover()

    with _ui_lock:
        client = _ui_mqtt
    # Published OUTSIDE _ui_lock, and this is load-bearing: publish_command()
    # can force a reconnect, whose on_connected flush runs on the paho network
    # thread and takes _ui_lock. Holding it across the publish parked that
    # thread -- so the PUBACK the republish was waiting for could never be
    # processed and the repair failed by construction, every single time.
    if client is not None and client.publish_command(device_id, payload):
        _log_ui("sent command %s to %s" % (cmd, device_id))
        # Only remote devices are tracked for an ack timeout: the own device's
        # buttons are never greyed, so a missing self-ack must not flag it as
        # "no response". Armed here, not on the tap, so the 60s window starts
        # when the command actually left the device.
        if not is_own:
            _arm_ack_timeout(device_id, cmd)
        _emit("commandResult", device_id, cmd, "sent")
        return

    _log_ui("could not send %s to %s: MQTT offline" % (cmd, device_id))
    # A RING that never left must not leave the button showing STOP.
    if cmd == "RING":
        _clear_ringing(device_id)
        _emit("devicesUpdated")
    if _queue_pending_command(device_id, cmd, payload):
        _emit("commandResult", device_id, cmd, "pending")
        return
    _emit("commandResult", device_id, cmd, "mqtt_offline")


def _queue_pending_command(device_id, cmd, payload):
    """Park an undeliverable command for the on_connected flush.

    Returns True if it was queued, i.e. the user can be told it is still on its
    way. Only _RETRY_COMMANDS qualify -- see there for why."""
    if cmd not in _RETRY_COMMANDS:
        return False
    with _ui_lock:
        _pending_commands[device_id] = (cmd, payload,
                                        time.time() + _PENDING_CMD_TTL_S)
    log.info("queued %s for %s until reconnect", cmd, device_id)
    return True


def _flush_pending_commands():
    """Re-send commands parked while offline. Runs in the paho network thread."""
    now = time.time()
    with _ui_lock:
        client = _ui_mqtt
        due = [(dev_id, cmd, payload)
               for dev_id, (cmd, payload, deadline) in _pending_commands.items()
               if deadline > now]
        stale = [(dev_id, cmd)
                 for dev_id, (cmd, _payload, deadline) in _pending_commands.items()
                 if deadline <= now]
        _pending_commands.clear()
    for dev_id, cmd in stale:
        _log_ui("dropped queued %s for %s (too old)" % (cmd, dev_id))
    if client is None:
        return
    for dev_id, cmd, payload in due:
        # wait=False is mandatory here for the same reason as in
        # _flush_pending_location: this runs on the paho network thread, and
        # waiting for the PUBACK would block the thread that has to process it.
        if client.publish_command(dev_id, payload, wait=False):
            _log_ui("sent queued command %s to %s" % (cmd, dev_id))
            _emit("commandResult", dev_id, cmd, "sent")
        else:
            # Dropped rather than re-parked: this already IS the retry, and a
            # STOP_RING that keeps chasing reconnects would outlive the ring.
            _log_ui("queued %s for %s could not be sent" % (cmd, dev_id))
            _emit("commandResult", dev_id, cmd, "mqtt_offline")


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
# Daemon status (Settings overview) -- derived from DB heartbeats only.
# The sandboxed GUI never talks to systemd; the daemons run permanently and
# steer themselves (idle/active) from the shared settings (see fmd/runtime.py).
# =========================================================================
def _background_daemon_owns_gps():
    """True when the GPS daemon is expected to poll: background activity is on
    AND the daemon package heartbeats (running or about to pick the flag up).
    In degraded mode (flag set but no daemon installed) the app must keep
    polling in the foreground so tracking does not silently stop."""
    return (settings.get_bool(settings.BACKGROUND_ENABLED)
            and runtime.daemon_status("gps") != "not_installed_or_stopped")


def _apply_foreground_gps():
    """Start/stop the in-app GPS polling to complement the background daemon.

    When "Background activity" is off (or the daemon package is not installed),
    the running app polls GPS itself so the location keeps being published
    until the user closes it. When the daemon owns the polling, the app stays
    quiet."""
    if _background_daemon_owns_gps():
        _stop_foreground_gps()
    else:
        _start_foreground_gps()


def get_daemon_status():
    """Status for the Settings page, purely from DB heartbeats.

    Per-daemon states: 'running' | 'deactivated' (daemon idles, features off)
    | 'applying' (daemon still naps on the settings from before the last save)
    | 'not_installed_or_stopped' (no fresh heartbeat). Adds the installed
    daemon version, the bundled RPM info and an update flag."""
    snap = runtime.daemon_snapshot()
    gps = snap["gps"]["status"]
    cmd = snap["cmd"]["status"]
    installed = (gps != "not_installed_or_stopped"
                 or cmd != "not_installed_or_stopped")
    daemon_version = snap["gps"]["version"] or snap["cmd"]["version"]
    bundle = daemon_rpm_available()
    update_available = bool(installed and bundle["available"] and daemon_version
                            and bundle["version"] != daemon_version)
    return {
        "gps": gps,
        "cmd": cmd,
        "installed": installed,
        "daemon_version": daemon_version,
        "bundled_available": bundle["available"],
        "bundled_version": bundle["version"],
        "update_available": update_available,
        "banner_needed": (not installed
                          and runtime.get("daemon_hint_dismissed") != "1"),
    }


def dismiss_daemon_banner():
    """Persist that the first-start 'install the background service' banner
    was dismissed; it never comes back."""
    runtime.set("daemon_hint_dismissed", "1")
    return {"ok": True}


def enable_location_now():
    """Ask for the system location services to be enabled (GPS prompt).

    The sandboxed GUI cannot write location.conf; it queues the wish for the
    cmd daemon, which forwards it to the root priv service. Fire-and-forget:
    without an installed daemon nothing happens (the UI communicates that)."""
    runtime.request_location_enable()
    _log_ui("location enable requested (queued for the background service)")
    return {"ok": True, "pending": True}


# =========================================================================
# Daemon RPM sideload (Settings install/update flow)
# =========================================================================
# The Store build bundles the daemon RPM inside the app's own share dir
# (readable under Sailjail). Chum builds ship no bundle: there the user
# installs harbour-find-my-device-daemon from the same repository instead.
_BUNDLED_RPM_DIR = "/usr/share/harbour-find-my-device/daemon"
_DAEMON_RPM_RE = r"^harbour-find-my-device-daemon-(.+?)\.[^.]+\.rpm$"


_bundled_rpm_cache = None


def daemon_rpm_available():
    """Info about the bundled daemon RPM:
    {'available': bool, 'path': str, 'version': 'X.Y-Z'}.

    Cached for the process lifetime: the RPM ships inside the app's own
    (read-only) install directory and cannot change while the app runs, but
    get_daemon_status() -- and with it this glob -- is polled every couple of
    seconds while the Settings page is open."""
    global _bundled_rpm_cache
    if _bundled_rpm_cache is not None:
        return _bundled_rpm_cache
    import glob
    import os
    import re
    rpms = sorted(glob.glob(os.path.join(
        _BUNDLED_RPM_DIR, "harbour-find-my-device-daemon-*.rpm")))
    if not rpms:
        _bundled_rpm_cache = {"available": False, "path": "", "version": ""}
    else:
        path = rpms[-1]
        m = re.match(_DAEMON_RPM_RE, os.path.basename(path))
        _bundled_rpm_cache = {"available": True, "path": path,
                              "version": m.group(1) if m else ""}
    return _bundled_rpm_cache


def stage_daemon_rpm():
    """Copy the bundled daemon RPM into ~/Downloads and return its file:// URL.

    The QML side opens that URL (Qt.openUrlExternally) so the system installer
    dialog takes over -- the flow the Harbour FAQ explicitly allows. The copy
    is needed because the installer cannot read paths that only exist inside
    the app sandbox view; ~/Downloads is shared via the Downloads permission."""
    import os
    import shutil
    bundle = daemon_rpm_available()
    if not bundle["available"]:
        return {"ok": False, "error": "no_bundled_rpm", "url": ""}
    downloads = os.path.expanduser("~/Downloads")
    try:
        os.makedirs(downloads, exist_ok=True)
        target = os.path.join(downloads, os.path.basename(bundle["path"]))
        shutil.copy2(bundle["path"], target)
    except OSError as exc:
        log.error("could not stage daemon RPM: %s", exc)
        return {"ok": False, "error": str(exc), "url": ""}
    _log_ui("daemon RPM staged for install: %s" % target)
    return {"ok": True, "error": "", "url": "file://" + target}


# =========================================================================
# UI MQTT listener (remote-device locations + acks)
# =========================================================================
def _start_ui_mqtt():
    """(Re)start the persistent UI MQTT client based on current settings, or
    tear a running one down when MQTT is disabled/unconfigured."""
    global _ui_mqtt
    enabled = settings.get_bool(settings.MQTT_ENABLED)
    server = settings.get(settings.MQTT_SERVER)
    if not enabled or not server or not mqtt_client.paho_available():
        with _ui_lock:
            old = _ui_mqtt
            _ui_mqtt = None
        if old is not None:
            # close(), not disconnect(): a GPS tick still holding the old
            # reference must not be able to revive it via the publish repair
            # path (client-id fight with any successor). Outside the lock: it
            # joins the network thread (see the restart path below).
            old.close()
        log.info("MQTT %s; UI listener %s",
                 "disabled in settings" if not enabled else "not configured",
                 "stopped" if old is not None else "not started")
        return
    tls = settings.get_bool(settings.MQTT_TLS)
    port = settings.get_int(settings.MQTT_PORT, 8883 if tls else 1883)
    own_id = devices.own_device_id()

    # Retire the predecessor OUTSIDE _ui_lock: close() joins the old network
    # thread for up to 2s, and the successor's on_connected (paho network
    # thread) takes this very lock -- holding it across a blocking call is the
    # pattern that parked that thread and broke the publish repair. Still done
    # before the successor connects, so the two never fight over the client id.
    with _ui_lock:
        old = _ui_mqtt
        _ui_mqtt = None
    if old is not None:
        old.close()  # retire for good; see teardown branch above

    client = mqtt_client.FmdMqttClient(
        server, port, tls,
        settings.get(settings.MQTT_USERNAME),
        settings.get(settings.MQTT_PASSWORD),
        mqtt_client.client_id(own_id, mqtt_client.ROLE_UI),
        on_location=_on_remote_location,
        on_ack=_on_remote_ack,
        on_connected=_on_ui_connected)
    # Published before connect(): on_connected can fire while we are still
    # subscribing below, and its flush must find this client, not the retired one.
    with _ui_lock:
        _ui_mqtt = client
    if client.connect():
        # Listen to every remote device's location + ack topics.
        for dev in devices.list_devices():
            if dev["is_own"] == 1:
                continue
            client.subscribe_location(dev["device_id"])
            client.subscribe_ack(dev["device_id"])
        # Also listen to the own ack topic: the command daemon acks here when it
        # executes a locally-triggered command (e.g. a RING sent from another
        # device), which lets us refresh the own-device row's STOP button.
        client.subscribe_ack(own_id)
        log.info("UI MQTT listener started")


def _on_ui_connected():
    """on_connected callback of the UI client: flush everything that was parked
    while it was offline. Runs in the paho network thread, so every publish in
    here must use wait=False."""
    _flush_pending_location()
    _flush_pending_commands()


def _flush_pending_location():
    """Publish the newest fix that was taken while the UI client was offline.

    Runs in the paho network thread via the on_connected callback, right after
    the subscriptions were re-applied."""
    global _pending_location
    with _ui_lock:
        if _pending_location is None or _ui_mqtt is None:
            return
        own_id, payload = _pending_location
        log.info("publishing fix taken while offline")
        # wait=False is mandatory here: this callback runs in the paho network
        # thread, and waiting for the PUBACK would block exactly the thread
        # that has to process it (guaranteed timeout + 5s stalled loop).
        if _ui_mqtt.publish_location(own_id, payload, wait=False):
            _pending_location = None


def _restart_ui_mqtt():
    """Restart the UI MQTT client off-thread. Disconnect can wait ~2s on a stuck
    network thread and connect resolves DNS, neither may stall the PyOtherSide
    worker (device add/edit/remove call this directly)."""
    threading.Thread(target=_restart_ui_mqtt_sync, daemon=True).start()

def _restart_ui_mqtt_sync():
    with _settings_apply_lock:
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
