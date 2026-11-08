# -*- coding: utf-8 -*-
"""icloud_bridge.py -- Apple iCloud "Find My" -> MQTT bridge.

Every POLL_INTERVAL seconds the bridge reads the locations of your own
iPhones/iPads and those of your Family Sharing group via pyicloud (every
LOCATE_INTERVAL seconds it additionally triggers an active locate; otherwise
it only reads the location Apple already has) and publishes them retained to
fmd/<device_id>, using the payload format of the Jolla/Sailfish app
"harbour-find-my-device".

It also listens for commands on fmd/<device_id>/cmd (RING, GPS), verifies the
HMAC token exactly like the Jolla app does and replies on
fmd/<device_id>/cmd/ack.

Usage:
  python icloud_bridge.py           run as daemon
  python icloud_bridge.py --login   one-time interactive login incl. 2FA
  python icloud_bridge.py --list    show all devices found in the account
                                    (--raw: dump all raw fields)
  python icloud_bridge.py --locate  actively locate first, like the GPS command)

"""

import datetime
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import signal
import string
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAuthRequiredException,
    PyiCloudFailedLoginException,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
try:
    import bridge_config as config
except ImportError:
    sys.exit("bridge_config.py not found: copy bridge_config.example.py to bridge_config.py and fill it in.")

log = logging.getLogger("icloud_bridge")

# Commands known to the Jolla app. Known but unsupported -> "disabled",
# unknown -> "error" (same behaviour as daemon_cmd.py of the Jolla app).
JOLLA_COMMANDS = ("RING", "STOP_RING", "LOCK", "GPS", "CAMERA", "DELETE")
ALLOWED_COMMANDS = ("RING", "GPS")

# Bridge status values (see report_status)
OK = "OK"
FA_REQUIRED = "2FA_REQUIRED"
LOGIN_FAILED = "LOGIN_FAILED"
ERROR = "ERROR"

_stop = threading.Event()
_icloud_lock = threading.RLock()     # pyicloud is not thread-safe: poll loop + command worker
_api = None                          # PyiCloudService, None while not logged in
_mqtt_client = None
_jobs = queue.Queue()
_last_timestamp = {}                 # apple_id -> timeStamp of the last published location
_last_locate = 0.0                   # time of the last active locate (poll or GPS command)
_reported_status = None


def path(name):
    """Resolves a path relative to the script directory."""
    return name if os.path.isabs(name) else os.path.join(BASE_DIR, name)


def setup_logging():
    log.setLevel(logging.INFO)
    log.propagate = False
    formatter = logging.Formatter('%(asctime)s %(levelname)s - %(message)s')
    file_handler = RotatingFileHandler(path(config.LOGFILE), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    log.addHandler(console_handler)


# --- Status notification --------------------------------------------------------
def report_status(status):
    """Called after every poll with the current bridge status (OK, 2FA_REQUIRED,
    LOGIN_FAILED, ERROR); acts only when the status changed.

    By default the change is only logged. Plug in your own notification here
    (push service, e-mail, home automation, ...) -- most importantly for
    2FA_REQUIRED, because the bridge then needs a manual --login.
    """
    global _reported_status
    if status is None or status == _reported_status:
        return
    log.info("bridge status: %s", status)
    _reported_status = status

    # --- Example: set an openHAB String item via its REST API -----------------
    # Requires URL_OPENHAB in bridge_config.py and `pip install requests`.
    # A failed request is retried on the next poll.
    #
    # try:
    #     import requests
    #     headers = {"Content-Type": "text/plain", "accept": "application/json"}
    #     response = requests.put(config.URL_OPENHAB, status, headers=headers, timeout=30)
    #     log.info("openHAB status %s -> HTTP %s", status, response.status_code)
    #     if not response.ok:
    #         _reported_status = None
    # except Exception as error:
    #     # the notification must never break the bridge
    #     log.error("openHAB not reachable: %s", error)
    #     _reported_status = None


# --- Device file (icloud_devices.json) -----------------------------------------
class DeviceFile:
    """Mapping Apple device id -> device_id, PIN, enabled.

    The file may be edited by hand (enabled, device_id). It is re-read before
    every poll if it has changed, and read fresh from disk before every write,
    so manual edits are never overwritten.
    """

    def __init__(self, filename):
        self.filename = path(filename)
        self.lock = threading.RLock()
        self.devices = {}
        self.mtime = None
        self._new = {}           # apple_id -> complete new entry
        self._info = {}          # apple_id -> {"name": ..., "model": ...}

    def _read(self):
        try:
            with open(self.filename, "r", encoding="utf-8") as f:
                content = json.load(f)
            return content if isinstance(content, dict) else {}
        except FileNotFoundError:
            return {}

    def _get_mtime(self):
        try:
            return os.path.getmtime(self.filename)
        except OSError:
            return None

    def _apply(self, new):
        """Sets the new state and returns (disabled, enabled) as sets of device_ids.
        A device_id changed by hand counts as "old one disabled, new one enabled"."""
        before = self.active_ids()
        self.devices = new
        after = self.active_ids()
        return before - after, after - before

    def reload(self, force=False):
        """Reads the file if it has changed. Returns (disabled, enabled)."""
        with self.lock:
            mtime = self._get_mtime()
            if not force and mtime == self.mtime:
                return set(), set()
            try:
                content = self._read()
            except (OSError, ValueError) as error:
                log.error("%s not readable, keeping previous state: %s", self.filename, error)
                return set(), set()
            self.mtime = mtime
            return self._apply(content)

    def save(self):
        """Writes new devices and name/model changes. Returns (disabled, enabled)."""
        with self.lock:
            if not self._new and not self._info:
                return set(), set()
            try:
                content = self._read()
            except (OSError, ValueError) as error:
                log.error("%s not readable, not saving: %s", self.filename, error)
                return set(), set()
            for apple_id, entry in self._new.items():
                content.setdefault(apple_id, entry)
            for apple_id, info in self._info.items():
                if apple_id in content:
                    content[apple_id].update(info)
            # write atomically: temp file + rename
            tmp = self.filename + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(content, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.filename)
            self._new, self._info = {}, {}
            self.mtime = self._get_mtime()
            return self._apply(content)

    def entry(self, apple_id, name, model):
        """Returns the entry for an Apple device, creating it if needed (written to disk by save())."""
        with self.lock:
            entry = self.devices.get(apple_id) or self._new.get(apple_id)
            if entry is None:
                entry = {
                    "device_id": self._new_id(),
                    "pin": str(secrets.randbelow(10 ** 6)).zfill(6),
                    "enabled": bool(config.NEW_DEVICES_ENABLED),
                    "name": name,
                    "model": model,
                    "first_seen": utc_text(time.time()),
                }
                self._new[apple_id] = entry
                log.info("New device: %s (%s) -> device_id %s, PIN %s, enabled %s",
                         name, model, entry["device_id"], entry["pin"], entry["enabled"])
            elif entry.get("name") != name or entry.get("model") != model:
                self._info[apple_id] = {"name": name, "model": model}
            return entry

    def _new_id(self):
        """Random 10-character id from [A-Za-z0-9], as expected by the Jolla app."""
        existing = {e.get("device_id") for e in list(self.devices.values()) + list(self._new.values())}
        chars = string.ascii_letters + string.digits
        while True:
            device_id = "".join(secrets.choice(chars) for _ in range(10))
            if device_id not in existing:
                return device_id

    def by_device_id(self, device_id):
        with self.lock:
            for apple_id, entry in self.devices.items():
                if entry.get("device_id") == device_id:
                    return apple_id, entry
            return None, None

    def active_ids(self):
        with self.lock:
            return {e.get("device_id") for e in self.devices.values()
                    if e.get("enabled") and e.get("device_id")}

    def inactive_ids(self):
        with self.lock:
            return {e.get("device_id") for e in self.devices.values()
                    if not e.get("enabled") and e.get("device_id")}


_devices = None


# --- HMAC (identical to fmd/tokens.py of the Jolla app) --------------------------
HMAC_STEP = 30       # seconds per time bucket
HMAC_WINDOW = 1      # accepted buckets before/after the current one
HMAC_DIGITS = 16     # length of the hex token


def _hmac_message(cmd, arg, bucket):
    return "{}:{}:{}".format((cmd or "").upper(), (arg or "").lower(), bucket).encode()


def verify_token(pin, cmd, arg, token, when=None):
    """Token = first 16 hex chars of HMAC-SHA256(PIN, "CMD:arg:bucket"), 30 s buckets, +-1 bucket."""
    if not pin or not token:
        return False
    when = when if when is not None else time.time()
    base = int(when // HMAC_STEP)
    for w in range(-HMAC_WINDOW, HMAC_WINDOW + 1):
        mac = hmac.new(str(pin).encode(), _hmac_message(cmd, arg, base + w),
                       hashlib.sha256).hexdigest()[:HMAC_DIGITS]
        if hmac.compare_digest(mac, str(token)):
            return True
    return False


# --- Payload ------------------------------------------------------------------
def utc_text(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def build_message(device_id, data):
    """Builds the location payload in Jolla format. None if there is no location."""
    location = data.get("location")
    if not location or location.get("latitude") is None or location.get("longitude") is None:
        return None
    timestamp = location.get("timeStamp")
    t = timestamp / 1000.0 if timestamp else time.time()
    local = datetime.datetime.fromtimestamp(t, tz=ZoneInfo(config.TIMEZONE))

    battery = data.get("batteryLevel")
    battery = round(battery * 100) if isinstance(battery, (int, float)) and battery > 0 else None

    return {
        "device_id": device_id,
        "timestamp_utc": utc_text(t),
        "timestamp_local": local.isoformat(timespec="seconds"),   # with UTC offset, like Jolla
        "lat": location.get("latitude"),
        "lon": location.get("longitude"),
        "alt": location.get("altitude"),
        "speed": 0.0,
        "accuracy": location.get("horizontalAccuracy"),
        "battery": battery,
        "source": "icloud",
        "name": data.get("name"),
    }


# --- iCloud -------------------------------------------------------------------
def login():
    return PyiCloudService(
        config.ICLOUD_USER, config.ICLOUD_PASSWORD,
        cookie_directory=path(config.ICLOUD_COOKIE_DIR),
        with_family=True,
        refresh_interval=None,   # no background thread in pyicloud, we poll ourselves
        locate=False,
    )


def session_stamp():
    """Latest modification in the cookie directory -> detects a --login run in the meantime."""
    directory = path(config.ICLOUD_COOKIE_DIR)
    try:
        return max((os.path.getmtime(os.path.join(directory, f)) for f in os.listdir(directory)),
                   default=None)
    except OSError:
        return None


def apple_devices(locate):
    """Refreshes the data at Apple and returns [(apple_id, data), ...] of the relevant devices."""
    with _icloud_lock:
        if _api is None:
            raise PyiCloudAuthRequiredException("not logged in")
        manager = _api.devices
        manager.refresh(locate=locate)
        return [(apple_id, dict(device.data)) for apple_id, device in manager.devices.items()
                if device.data.get("deviceClass") in config.DEVICE_CLASSES]


# --- MQTT ---------------------------------------------------------------------
def start_mqtt():
    global _mqtt_client
    client_id = "icloud_bridge_" + secrets.token_hex(4)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)  # paho-mqtt 2.x
    except AttributeError:
        client = mqtt.Client(client_id=client_id)                                   # paho-mqtt 1.x
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    if config.MQTT_TLS:
        client.tls_set()
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect_async(config.MQTT_SERVER, config.MQTT_PORT, keepalive=60)
    client.loop_start()
    _mqtt_client = client


def _on_connect(client, userdata, flags, rc):
    if rc != 0:
        log.error("MQTT connection refused, rc=%s", rc)
        return
    ids = _devices.active_ids()
    log.info("MQTT connected, subscribing to %d devices", len(ids))
    for device_id in ids:
        client.subscribe("fmd/%s/cmd" % device_id, qos=1)


def _on_message(client, userdata, message):
    # Runs in the paho network thread: only enqueue here, the worker executes it.
    parts = message.topic.split("/")
    if len(parts) == 3 and parts[0] == "fmd" and parts[2] == "cmd":
        _jobs.put(("cmd", parts[1], message.payload))


def subscribe(device_ids, on):
    for device_id in device_ids:
        topic = "fmd/%s/cmd" % device_id
        if on:
            _mqtt_client.subscribe(topic, qos=1)
            log.info("subscribed: %s", topic)
        else:
            _mqtt_client.unsubscribe(topic)
            log.info("unsubscribed: %s", topic)


def clear_location(device_ids):
    """Empty retained publish -> the broker forgets the last location."""
    for device_id in device_ids:
        _mqtt_client.publish("fmd/%s" % device_id, payload=b"", qos=1, retain=True)
        log.info("retained location cleared: fmd/%s", device_id)


def publish_location(message):
    _mqtt_client.publish("fmd/%s" % message["device_id"], json.dumps(message), qos=1, retain=True)
    log.info("location published: %s (%s) %s, %s ±%sm battery %s",
             message["device_id"], message["name"], message["lat"], message["lon"],
             message["accuracy"], message["battery"])


def send_ack(device_id, cmd, result):
    _mqtt_client.publish("fmd/%s/cmd/ack" % device_id, json.dumps({"cmd": cmd, "result": result}), qos=1)
    log.info("ack: %s %s -> %s", device_id, cmd, result)


def apply_changes(change):
    disabled, enabled = change
    if disabled:
        subscribe(disabled, False)
        clear_location(disabled)
    if enabled:
        subscribe(enabled, True)


# --- Locations ----------------------------------------------------------------
def publish_locations(device_list, only_apple_id=None, force=False):
    """Maps the Apple devices to their entries and publishes changed locations."""
    for apple_id, data in device_list:
        if only_apple_id and apple_id != only_apple_id:
            continue
        entry = _devices.entry(apple_id, data.get("name"), data.get("deviceDisplayName"))
        if not entry.get("enabled"):
            continue
        message = build_message(entry["device_id"], data)
        if message is None:
            log.info("no location for %s (%s)", entry["device_id"], data.get("name"))
            continue
        # only publish if Apple reports a newer location than last time
        timestamp = (data.get("location") or {}).get("timeStamp")
        if not force and timestamp and _last_timestamp.get(apple_id) == timestamp:
            continue
        _last_timestamp[apple_id] = timestamp
        publish_location(message)
    apply_changes(_devices.save())


# --- Commands -----------------------------------------------------------------
def job_worker():
    while not _stop.is_set():
        try:
            job = _jobs.get(timeout=1)
        except queue.Empty:
            continue
        try:
            if job[0] == "cmd":
                handle_command(job[1], job[2])
            elif job[0] == "gps_followup":
                publish_locations(apple_devices(locate=False), only_apple_id=job[1], force=True)
            elif job[0] == "locate_followup":
                publish_locations(apple_devices(locate=False))
        except Exception:
            log.exception("job %s failed", job[:2])


def handle_command(device_id, raw):
    apple_id, entry = _devices.by_device_id(device_id)
    if entry is None or not entry.get("enabled"):
        log.info("command for unknown/disabled device %s ignored", device_id)
        return
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        log.warning("command for %s is not JSON, ignored", device_id)
        return
    if not isinstance(payload, dict):
        log.warning("command for %s is not a JSON object, ignored", device_id)
        return

    # Anyone may publish here -> sanitize values before using them (like Jolla).
    cmd = str(payload.get("cmd") or "").upper()
    arg = payload.get("arg")
    if arg is not None and not isinstance(arg, str):
        arg = str(arg)

    # 1. token  2. command allowed?  3. execute  (same order as daemon_cmd.py)
    if not verify_token(entry.get("pin"), cmd, arg, payload.get("token")):
        log.warning("command %s for %s: token verification FAILED", cmd, device_id)
        send_ack(device_id, cmd, "auth_failed")
        return
    if cmd not in ALLOWED_COMMANDS:
        send_ack(device_id, cmd, "disabled" if cmd in JOLLA_COMMANDS else "error")
        return

    try:
        with _icloud_lock:
            if _api is None:
                raise PyiCloudAuthRequiredException("not logged in")
            if cmd == "RING":
                _api.devices.devices[apple_id].play_sound()
            elif cmd == "GPS":
                _api.devices.refresh(locate=True)
                remember_locate()
        result = "ok"
    except Exception:
        log.exception("command %s for %s failed", cmd, device_id)
        result = "error"
    send_ack(device_id, cmd, result)

    if cmd == "GPS" and result == "ok":
        # Locating takes a few seconds; publish the fresh location afterwards.
        timer = threading.Timer(config.GPS_FOLLOWUP, _jobs.put, args=(("gps_followup", apple_id),))
        timer.daemon = True
        timer.start()


# --- Daemon -------------------------------------------------------------------
def locate_interval():
    """Seconds between two active locates, 0 = never."""
    return max(0, int(getattr(config, "LOCATE_INTERVAL", 0) or 0))


def remember_locate():
    global _last_locate
    _last_locate = time.time()


def locate_due():
    interval = locate_interval()
    return interval > 0 and time.time() - _last_locate >= interval


def daemon():
    global _api, _devices
    setup_logging()
    log.info("iCloud bridge started ...")
    signal.signal(signal.SIGTERM, lambda *args: _stop.set())

    _devices = DeviceFile(config.DEVICES_FILE)
    _devices.reload(force=True)
    start_mqtt()
    clear_location(_devices.inactive_ids())      # on every start, to be safe
    threading.Thread(target=job_worker, name="jobs", daemon=True).start()

    status = None
    errors_in_a_row = 0
    last_login = 0.0
    login_stamp = None

    while not _stop.is_set():
        apply_changes(_devices.reload())

        # Log in. With 2FA pending, only retry once --login has renewed the session
        # (otherwise every password login triggers a 2FA prompt on all Apple devices).
        if _api is None:
            stamp = session_stamp()
            if status is None \
                    or (status == FA_REQUIRED and stamp != login_stamp) \
                    or (status == LOGIN_FAILED and time.time() - last_login >= config.SESSION_RECHECK) \
                    or (status == ERROR and time.time() - last_login >= config.POLL_INTERVAL):
                last_login = time.time()
                login_stamp = stamp
                try:
                    new = login()
                    if new.requires_2fa or new.requires_2sa:
                        status = FA_REQUIRED
                        log.error("iCloud session expired: please run 'python icloud_bridge.py --login'")
                    else:
                        with _icloud_lock:
                            _api = new
                        log.info("iCloud logged in")
                except PyiCloudFailedLoginException as error:
                    status = LOGIN_FAILED
                    log.error("iCloud login failed: %s", error)
                except PyiCloud2FARequiredException:
                    status = FA_REQUIRED
                    log.error("iCloud session expired: please run 'python icloud_bridge.py --login'")
                except Exception as error:
                    errors_in_a_row += 1
                    log.error("iCloud login not possible: %s", error)
                login_stamp = session_stamp()

        if _api is not None:
            try:
                locate = locate_due()
                publish_locations(apple_devices(locate=locate))
                if locate:
                    remember_locate()
                    log.info("active locate requested, follow-up query in %d s", config.GPS_FOLLOWUP)
                    # The devices report their new position to Apple only after a few seconds.
                    timer = threading.Timer(config.GPS_FOLLOWUP, _jobs.put, args=(("locate_followup",),))
                    timer.daemon = True
                    timer.start()
                errors_in_a_row = 0
                status = OK
            except (PyiCloud2FARequiredException, PyiCloudAuthRequiredException) as error:
                log.error("iCloud requires a new login (%s): please run 'python icloud_bridge.py --login'", error)
                with _icloud_lock:
                    _api = None
                status = FA_REQUIRED
                login_stamp = session_stamp()
            except PyiCloudFailedLoginException as error:
                log.error("iCloud login failed: %s", error)
                with _icloud_lock:
                    _api = None
                status = LOGIN_FAILED
            except Exception as error:
                errors_in_a_row += 1
                log.error("query failed (%d in a row): %s", errors_in_a_row, error)

        if errors_in_a_row >= config.ERROR_THRESHOLD and status not in (FA_REQUIRED, LOGIN_FAILED):
            status = ERROR
            if _api is not None:
                # rebuild the session in case it is stuck in a bad state
                with _icloud_lock:
                    _api = None
                last_login = time.time()
        report_status(status)

        _stop.wait(config.POLL_INTERVAL)

    log.info("iCloud bridge stopped.")
    if _mqtt_client is not None:
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()


# --- Helper modes -------------------------------------------------------------
def interactive_login():
    try:
        api = login()
    except PyiCloudFailedLoginException as error:
        sys.exit("Login failed (Apple ID/password?): %s" % error)

    if api.requires_2fa:
        if api.security_key_names:
            sys.exit("The account requires a security key (FIDO2); this is not supported by the bridge.")
        api.request_2fa_code()
        code = input("2FA code (shown on your Apple device or sent via SMS): ").strip()
        if not api.validate_2fa_code(code):
            sys.exit("2FA code was not accepted.")
        if not api.is_trusted_session and not api.trust_session():
            print("Note: Apple did not mark the session as trusted, "
                  "you may have to log in again soon.")
    elif api.requires_2sa:
        sys.exit("The account uses the legacy two-step verification (2SA); this is not supported by the bridge.")

    print("Logged in. Session saved in %s" % path(config.ICLOUD_COOKIE_DIR))
    print("A running bridge picks up the new session within %d s." % config.POLL_INTERVAL)


def list_devices(raw, locate):
    try:
        api = login()
    except PyiCloudFailedLoginException as error:
        sys.exit("Login failed: %s" % error)
    if api.requires_2fa or api.requires_2sa:
        sys.exit("Not logged in: please run 'python icloud_bridge.py --login' first.")
    if locate:
        # like the GPS command: locate actively, wait a moment, then fetch fresh data
        api.devices.refresh(locate=True)
        print("Locate requested, waiting %d s ..." % config.GPS_FOLLOWUP)
        time.sleep(config.GPS_FOLLOWUP)
        api.devices.refresh(locate=False)

    mapping = DeviceFile(config.DEVICES_FILE)
    mapping.reload(force=True)
    for apple_id, device in api.devices.devices.items():
        data = device.data
        entry = mapping.devices.get(apple_id, {})
        location = data.get("location") or {}
        print("-" * 70)
        print("Name:          %s" % data.get("name"))
        print("Model:         %s / %s (%s)" % (data.get("deviceDisplayName"), data.get("rawDeviceModel"),
                                               data.get("deviceClass")))
        print("Apple id:      %s" % apple_id)
        print("discoveryId:   %s" % data.get("deviceDiscoveryId"))
        print("prsId:         %s" % data.get("prsId"))
        print("Battery:       %s (%s)" % (data.get("batteryLevel"), data.get("batteryStatus")))
        if location:
            print("Location:      %s, %s ±%sm, %s, isOld=%s" % (
                location.get("latitude"), location.get("longitude"), location.get("horizontalAccuracy"),
                utc_text(location["timeStamp"] / 1000.0) if location.get("timeStamp") else "?",
                location.get("isOld")))
        else:
            print("Location:      -")
        print("Bridge:        %s" % ("included" if data.get("deviceClass") in config.DEVICE_CLASSES
                                     else "ignored (device class)"))
        if entry:
            print("Mapping:       device_id %s, enabled %s" % (entry.get("device_id"), entry.get("enabled")))
        if raw:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    if "--login" in sys.argv:
        interactive_login()
    elif "--list" in sys.argv:
        list_devices(raw="--raw" in sys.argv, locate="--locate" in sys.argv)
    else:
        try:
            daemon()
        except KeyboardInterrupt:
            _stop.set()
