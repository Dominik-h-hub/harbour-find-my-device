# -*- coding: utf-8 -*-
"""modules.py -- MQTT subscriber, device store, command tokens.

Protocol (see the app's qml/utilities/mqtt_client.py and fmd/tokens.py):

  fmd/<id>          location JSON, retained, QoS 1
  fmd/<id>/cmd      command JSON {cmd, arg, token}, QoS 1
  fmd/<id>/cmd/ack  ack JSON {cmd, result}, QoS 1

The command token is HMAC-SHA256(secret=PIN, "CMD:arg:timebucket"), first
16 hex chars, 30-second buckets (receiver accepts +/-1 bucket).
"""

import hashlib
import hmac
import json
import threading
import time

import paho.mqtt.client as mqtt

import config

# --- in-memory device store ------------------------------------------------
_lock = threading.Lock()
_devices = {}       # device_id -> dict with location fields + last_ack
_client = None


def _device(device_id):
    """Get-or-create the store entry for a device (call with _lock held)."""
    return _devices.setdefault(device_id, {"device_id": device_id})


def all_devices():
    """Snapshot of all known devices, sorted by id."""
    with _lock:
        return [dict(d) for _, d in sorted(_devices.items())]


# --- command token (must match fmd/tokens.py on the phone) -----------------
HMAC_STEP = 30
HMAC_DIGITS = 16


def make_command_token(pin, cmd, arg=None, when=None):
    when = time.time() if when is None else when
    bucket = int(when // HMAC_STEP)
    msg = "{}:{}:{}".format((cmd or "").upper(), (arg or "").lower(), bucket)
    return hmac.new(str(pin).encode(), msg.encode(),
                    hashlib.sha256).hexdigest()[:HMAC_DIGITS]


# --- MQTT ------------------------------------------------------------------
def start_mqtt():
    """Connect to the broker and subscribe fmd/# in a background thread."""
    global _client
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)  # paho-mqtt 2.x
    except AttributeError:
        client = mqtt.Client()                                  # paho-mqtt 1.x
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    if config.MQTT_TLS:
        client.tls_set()
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.connect(config.MQTT_SERVER, config.MQTT_PORT, keepalive=60)
    client.loop_start()
    _client = client


def _on_connect(client, userdata, flags, rc):
    client.subscribe("fmd/#", qos=1)


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    parts = msg.topic.split("/")
    if len(parts) == 2:                                   # fmd/<id>: location
        with _lock:
            _device(parts[1]).update(payload)
    elif len(parts) == 4 and parts[2:] == ["cmd", "ack"]:  # fmd/<id>/cmd/ack
        with _lock:
            _device(parts[1])["last_ack"] = "%s -> %s" % (
                payload.get("cmd"), payload.get("result"))
    # fmd/<id>/cmd is our own outgoing traffic; nothing to do.


def send_command(device_id, cmd, arg=None):
    """Publish a signed command; the result arrives on the ack topic."""
    pin = config.DEVICE_PINS.get(device_id)
    with _lock:
        dev = _device(device_id)
        if pin is None:
            dev["last_ack"] = "%s -> no PIN in config.py" % cmd
            return
        dev["last_ack"] = "%s -> sent, waiting for ack..." % cmd
    payload = {"cmd": (cmd or "").upper(), "arg": arg,
               "token": make_command_token(pin, cmd, arg)}
    _client.publish("fmd/%s/cmd" % device_id, json.dumps(payload), qos=1)


