#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mqtt_client.py -- MQTT wrapper for harbour-find-my-device.

Responsibilities:
  * Client ids:  <device-id>-pub / <device-id>-cmd / <device-id>-ui
  * Topics:      fmd/<id>            location, RETAIN=true,  QoS 1
                 fmd/<id>/cmd        commands,  RETAIN=false, QoS 1
                 fmd/<id>/cmd/ack    acks,      RETAIN=false, QoS 1
  * TLS optional (default on, port 8883; plain 1883).
  * Offline tolerant: connect() never raises; callers check is_connected().

This module is transport only -- it does NOT know about PINs, tokens or the DB.
The daemons wire payload building / auth on top of it.
"""

import json
import logging
import socket
import ssl

log = logging.getLogger("fmd.mqtt")

try:
    import paho.mqtt.client as mqtt
    _HAVE_PAHO = True
except Exception as _exc:
    mqtt = None
    _HAVE_PAHO = False
    log.warning("paho-mqtt not importable: %s (MQTT disabled until installed)", _exc)

QOS = 1
ROLE_PUB = "pub"
ROLE_CMD = "cmd"
ROLE_UI = "ui"


# --- topic helpers ---------------------------------------------------------
def topic_location(device_id):
    return "fmd/%s" % device_id


def topic_cmd(device_id):
    return "fmd/%s/cmd" % device_id


def topic_ack(device_id):
    return "fmd/%s/cmd/ack" % device_id


def client_id(device_id, role):
    return "%s-%s" % (device_id, role)


def paho_available():
    return _HAVE_PAHO


def network_up(host=None, port=None, timeout=3.0):
    """Best-effort connectivity probe (skip publishing when offline).

    If host/port are given, tries a TCP connect to the broker; otherwise just
    checks that a route to a public address can be resolved/opened. Returns bool.
    """
    try:
        if host:
            with socket.create_connection((host, int(port or 1883)), timeout=timeout):
                return True
        # No broker given: probe a well-known address (no data sent).
        with socket.create_connection(("8.8.8.8", 53), timeout=timeout):
            return True
    except OSError:
        return False


def _new_paho_client(cid, clean_session=True):
    """Create a paho Client across paho 1.x / 2.x callback-API differences."""
    try:
        # paho-mqtt 2.x requires an explicit callback API version.
        return mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=cid, clean_session=clean_session)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x
        return mqtt.Client(client_id=cid, clean_session=clean_session)


# --- client ----------------------------------------------------------------
class FmdMqttClient(object):
    """Thin convenience wrapper around one paho client connection.

    on_command(device_id, payload_dict)  -- called for messages on any subscribed
                                            fmd/<id>/cmd topic.
    on_location(device_id, payload_dict) -- called for messages on any subscribed
                                            fmd/<id> location topic.
    on_ack(device_id, payload_dict)      -- called for any subscribed
                                            fmd/<id>/cmd/ack topic.
    Subscriptions are remembered and re-applied on reconnect.
    """

    def __init__(self, server, port, tls, username, password, cid,
                 on_command=None, on_location=None, on_ack=None,
                 clean_session=True):
        self.server = server
        self.port = int(port)
        self.tls = bool(tls)
        self.username = username
        self.password = password
        self.cid = cid
        self.on_command = on_command
        self.on_location = on_location
        self.on_ack = on_ack
        self._client = None
        self._connected = False
        self._subs = set()              # set of (topic, kind)
        self._clean_session = clean_session

    # -- lifecycle --
    def connect(self):
        """Create the client and start the network loop. Never raises.

        Returns True if the connect was dispatched (not necessarily completed).
        """
        if not _HAVE_PAHO:
            log.error("cannot connect: paho-mqtt not installed")
            return False
        if not self.server:
            log.warning("no MQTT server configured; not connecting")
            return False
        try:
            self._client = _new_paho_client(self.cid, self._clean_session)
            if self.username:
                self._client.username_pw_set(self.username, self.password or "")
            if self.tls:
                self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED,
                                     tls_version=ssl.PROTOCOL_TLS)
            self._client.on_connect = self._handle_connect
            self._client.on_disconnect = self._handle_disconnect
            self._client.on_message = self._handle_message
            self._client.reconnect_delay_set(min_delay=1, max_delay=60)
            log.info("connecting to mqtt %s:%d (tls=%s) as %s",
                     self.server, self.port, self.tls, self.cid)
            self._client.connect_async(self.server, self.port, keepalive=45)
            self._client.loop_start()
            return True
        except Exception as exc:
            log.error("mqtt connect failed: %s", exc)
            return False

    def disconnect(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        self._connected = False
        log.info("mqtt disconnected (%s)", self.cid)

    def is_connected(self):
        return self._connected

    # -- subscriptions --
    def subscribe_commands(self, device_id):
        self._add_sub(topic_cmd(device_id), "cmd")

    def subscribe_location(self, device_id):
        self._add_sub(topic_location(device_id), "loc")

    def subscribe_ack(self, device_id):
        self._add_sub(topic_ack(device_id), "ack")

    def _add_sub(self, topic, kind):
        self._subs.add((topic, kind))
        if self._client is not None and self._connected:
            self._client.subscribe(topic, qos=QOS)
            log.info("subscribed %s (%s)", topic, kind)

    # -- publishing --
    def publish_location(self, device_id, payload):
        """Publish a location payload (retain=true, QoS1)."""
        return self._publish(topic_location(device_id), payload, retain=True)

    def publish_command(self, device_id, payload):
        """Publish a command to a remote device (retain=false, QoS1)."""
        return self._publish(topic_cmd(device_id), payload, retain=False)

    def publish_ack(self, device_id, payload):
        """Publish a command result on the ack topic (retain=false, QoS1)."""
        return self._publish(topic_ack(device_id), payload, retain=False)

    def _publish(self, topic, payload, retain):
        if not (self._client is not None and self._connected):
            log.warning("publish skipped (not connected): %s", topic)
            return False
        try:
            body = json.dumps(payload) if not isinstance(payload, str) else payload
            info = self._client.publish(topic, body, qos=QOS, retain=retain)
            log.info("published %s (retain=%s, mid=%s)", topic, retain,
                     getattr(info, "mid", "?"))
            return True
        except Exception as exc:
            log.error("publish to %s failed: %s", topic, exc)
            return False

    # -- paho callbacks --
    def _handle_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            log.info("mqtt connected (%s)", self.cid)
            for topic, _kind in self._subs:
                client.subscribe(topic, qos=QOS)
                log.info("re-subscribed %s", topic)
        else:
            self._connected = False
            log.error("mqtt connect refused rc=%s", rc)

    def _handle_disconnect(self, client, userdata, rc):
        self._connected = False
        log.warning("mqtt connection lost rc=%s (will auto-reconnect)", rc)

    def _handle_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            log.warning("non-JSON message on %s, ignored", topic)
            return
        device_id = _device_from_topic(topic)
        log.info("mqtt message on %s", topic)
        if topic.endswith("/cmd/ack"):
            if self.on_ack:
                self.on_ack(device_id, payload)
        elif topic.endswith("/cmd"):
            if self.on_command:
                self.on_command(device_id, payload)
        else:
            if self.on_location:
                self.on_location(device_id, payload)


def _device_from_topic(topic):
    """Extract <device-id> from fmd/<id>[/cmd[/ack]]."""
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "fmd" else None
