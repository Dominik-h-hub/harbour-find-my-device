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
import threading

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

# Keepalive: Sailfish suspends the CPU while the display is off, which freezes
# the paho network thread -- no PINGREQ leaves the device while it sleeps, and
# the broker drops the connection after 1.5x keepalive (45s meant a kick +
# full TLS reconnect every ~67s). 900s keeps the session alive across suspend
# as long as anything (e.g. a GPS tick) wakes the device within ~22 minutes.
# NOTE: handover detection does NOT run through this MQTT keepalive. On the
# publisher it works via verified publishes (PUBACK wait + forced reconnect)
# plus TCP_USER_TIMEOUT; on the idle subscriber (-cmd) via SO_KEEPALIVE and
# the ConnMan state signal. Lowering this value would only triple the PINGREQ
# radio wakeups without improving detection.
KEEPALIVE_S = 900

# Timeout for the QoS1 PUBACK when a publish is verified (_publish(wait=True)).
# wait_for_publish() polls internally at timeout/10, so keep this short: only
# the failure case pays for it, and 5s is plenty for a healthy link.
PUBLISH_ACK_TIMEOUT_S = 5

# OS-level TCP keepalive. The MQTT keepalive above is deliberately long, but a
# WLAN<->mobile handover leaves the old socket "half-open". Values chosen for
# an IDLE connection (the -cmd/-ui subscribers): probing a mostly-idle link
# once a minute (old value 60s) kept the radio permanently busy for nothing.
TCP_KEEPIDLE_S = 300     # start probing after 5 min idle
TCP_KEEPINTVL_S = 30     # then probe every 30s
TCP_KEEPCNT = 3          # give up (socket dead) after 3 missed probes

# TCP keepalive probes are only sent on an *idle* connection: as soon as
# unacknowledged data sits in the send buffer (exactly what happens when a
# publish is written into a half-open socket), the kernel switches to the
# retransmission timer (tcp_retries2 ~ 13-30 min) and keepalive stays silent.
# TCP_USER_TIMEOUT caps that: a connection with unacknowledged data is killed
# after this many milliseconds instead of retransmitting for minutes -- which
# also ends the repeated radio wakeups of those retransmit phases.
TCP_USER_TIMEOUT_MS = 25000  # 25s: half-open socket dies shortly after handover


def _enable_tcp_keepalive(sock):
    """Turn on OS TCP keepalive + TCP_USER_TIMEOUT on a (re)connect socket so a
    half-open socket left by a network handover is detected promptly instead of
    waiting out KEEPALIVE_S (idle case) or the kernel retransmit limit (data in
    flight; see TCP_USER_TIMEOUT_MS above for why SO_KEEPALIVE alone is not
    enough there)."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        return
    for opt_name, value in (("TCP_KEEPIDLE", TCP_KEEPIDLE_S),
                            ("TCP_KEEPINTVL", TCP_KEEPINTVL_S),
                            ("TCP_KEEPCNT", TCP_KEEPCNT)):
        opt = getattr(socket, opt_name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass
    # Linux-only; the constant is missing from some python builds, the numeric
    # fallback 18 is stable on Linux. Value is in milliseconds.
    try:
        sock.setsockopt(socket.IPPROTO_TCP,
                        getattr(socket, "TCP_USER_TIMEOUT", 18),
                        TCP_USER_TIMEOUT_MS)
    except OSError:
        pass


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
    result = []

    def _probe():
        try:
            if host:
                with socket.create_connection((host, int(port or 1883)),
                                              timeout=timeout):
                    result.append(True)
                return
            # No broker given: probe a well-known address (no data sent).
            with socket.create_connection(("8.8.8.8", 53), timeout=timeout):
                result.append(True)
        except OSError:
            pass

    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(timeout + 0.5)
    return bool(result)


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
    on_connected()                       -- called after every (re)connect, once
                                            the subscriptions are re-applied.
    Subscriptions are remembered and re-applied on reconnect.
    """

    def __init__(self, server, port, tls, username, password, cid,
                 on_command=None, on_location=None, on_ack=None,
                 clean_session=True, on_connected=None):
        self.server = server
        self.port = int(port)
        self.tls = bool(tls)
        self.username = username
        self.password = password
        self.cid = cid
        self.on_command = on_command
        self.on_location = on_location
        self.on_ack = on_ack
        self.on_connected = on_connected
        self._client = None
        self._connected = False
        self._closed = False
        self._subs = set()              # set of (topic, kind)
        self._clean_session = clean_session
        # Set on CONNACK rc=0, cleared on connect()/disconnect; wait_connected()
        # blocks on it (connect() is async: connect_async + loop_start).
        self._conn_event = threading.Event()
        # Serialises force_reconnect() so concurrent repair paths (publish
        # failure, ConnMan signal, net watch) never tear down the same client
        # twice in parallel.
        self._reconnect_lock = threading.Lock()

    # -- lifecycle --
    def connect(self):
        """Create the client and start the network loop. Never raises.

        Returns True if the connect was dispatched (not necessarily completed).
        """
        if self._closed:
            log.warning("connect refused: client closed (%s)", self.cid)
            return False
        if not _HAVE_PAHO:
            log.error("cannot connect: paho-mqtt not installed")
            return False
        if not self.server:
            log.warning("no MQTT server configured; not connecting (%s)", self.cid)
            return False
        self._conn_event.clear()
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
            self._client.on_socket_open = self._handle_socket_open
            self._client.reconnect_delay_set(min_delay=1, max_delay=60)
            log.info("connecting to mqtt %s:%d (tls=%s) as %s",
                     self.server, self.port, self.tls, self.cid)
            self._client.connect_async(self.server, self.port,
                                       keepalive=KEEPALIVE_S)
            self._client.loop_start()
            return True
        except Exception as exc:
            log.error("mqtt connect failed (%s): %s", self.cid, exc)
            return False

    def disconnect(self):
        old = self._client
        self._client = None
        self._connected = False
        self._conn_event.clear()
        if old is not None:
            # Unbind the callbacks first: an abandoned network thread must
            # never touch this wrapper again. A late on_disconnect from an old
            # client used to mark the freshly connected successor as offline
            # (see also the staleness guards in the _handle_* callbacks).
            try:
                old.on_connect = None
                old.on_disconnect = None
                old.on_message = None
                old.on_socket_open = None
            except Exception:
                pass
            try:
                # Not loop_stop(): that joins the network thread without a
                # timeout, and while offline that thread can sit in a DNS lookup
                # (getaddrinfo) for minutes during auto-reconnect. Signal it to
                # terminate and abandon it (daemon thread) if it doesn't exit in
                # time; connect() always builds a fresh client anyway.
                old._thread_terminate = True
            except Exception:
                pass
            try:
                old.disconnect()
            except Exception:
                pass
            try:
                # Close the socket to unstick a thread blocked in a TLS
                # read/handshake; together with _thread_terminate it then exits
                # instead of finishing a reconnect that would fight the
                # successor for the (identical) client id.
                sock = old.socket()
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            try:
                thread = getattr(old, "_thread", None)
                if thread is not None and thread is not threading.current_thread():
                    thread.join(2.0)
                    if thread.is_alive():
                        log.warning("mqtt network thread still busy; abandoned")
                    else:
                        old._thread = None
            except Exception:
                pass
        log.info("mqtt disconnected (%s)", self.cid)

    def close(self):
        """Permanently shut this client down. Unlike disconnect(), no repair
        path (force_reconnect from a late publish still holding this
        reference) can revive it afterwards -- a revived predecessor would
        fight its successor for the identical client id at the broker.
        Use this whenever the wrapper object is being replaced or retired."""
        self._closed = True
        self.disconnect()

    def is_connected(self):
        return self._connected

    def is_really_connected(self):
        """Connection state confirmed by paho, not just our wrapper flag.

        The wrapper flag alone is too optimistic: after a silent socket death
        paho may already know it is disconnected while _connected is still
        True. All health checks should use this."""
        return bool(self._connected and self._client is not None
                    and self._client.is_connected())

    def wait_connected(self, timeout=15):
        """Block until the CONNACK arrived (connect() is asynchronous).

        A publish right after connect() would otherwise always fail. Single
        event wait, no polling. Returns bool."""
        return self._conn_event.wait(timeout)

    def force_reconnect(self):
        """Hard-drop the current client and connect a fresh one. Never raises.

        Subscriptions are kept in self._subs and re-applied by _handle_connect.
        Serialised via _reconnect_lock so concurrent repair paths don't fight."""
        with self._reconnect_lock:
            try:
                log.warning("forcing mqtt reconnect (%s)", self.cid)
                self.disconnect()
                return self.connect()
            except Exception:
                log.exception("force_reconnect failed (%s)", self.cid)
                return False

    def local_ip(self):
        """Source address of the live MQTT socket, or None. Used by the net
        watch to detect that the kernel would now route via a different
        interface (WLAN<->mobile handover stranded this socket)."""
        try:
            sock = self._client.socket() if self._client is not None else None
            return sock.getsockname()[0] if sock is not None else None
        except (OSError, AttributeError, IndexError):
            return None

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
            log.info("subscribed %s (%s, %s)", topic, kind, self.cid)

    # -- publishing --
    def publish_location(self, device_id, payload, wait=True):
        """Publish a location payload (retain=true, QoS1)."""
        return self._publish(topic_location(device_id), payload, retain=True,
                             wait=wait)

    def publish_command(self, device_id, payload):
        """Publish a command to a remote device (retain=false, QoS1)."""
        return self._publish(topic_cmd(device_id), payload, retain=False)

    def publish_ack(self, device_id, payload):
        """Publish a command result on the ack topic (retain=false, QoS1)."""
        return self._publish(topic_ack(device_id), payload, retain=False)

    def _publish(self, topic, payload, retain, wait=True):
        """Publish with delivery verification and one self-repair attempt.

        wait=True (default): block until the QoS1 PUBACK arrived; on failure
        force a hard reconnect and republish the SAME payload exactly once
        (a reconnect alone would save the connection but lose this tick's
        position). Total worst case ~8s -- fine against a 5-min tick.

        wait=False MUST be used by any caller running in the paho network
        thread (e.g. an on_connected flush): waiting for the PUBACK there
        blocks exactly the thread that would process it, guaranteeing the
        timeout. The wait=False path also skips the reconnect/republish repair.
        """
        body = json.dumps(payload) if not isinstance(payload, str) else payload
        if not self.is_really_connected():
            if not wait:
                log.warning("publish skipped (not connected): %s (%s)",
                            topic, self.cid)
                return False
            log.warning("publish on dead connection; reconnecting first: %s (%s)",
                        topic, self.cid)
            if not (self.force_reconnect() and self.wait_connected()):
                return False
            return self._send_once(topic, body, retain, wait)
        if self._send_once(topic, body, retain, wait):
            return True
        if not wait:
            return False
        # First attempt failed (no PUBACK / error): hard reconnect, then
        # republish this payload exactly once. Only then give up (-> caller's
        # pending queue).
        log.warning("publish failed; reconnect + republish once: %s (%s)",
                    topic, self.cid)
        if not (self.force_reconnect() and self.wait_connected()):
            return False
        ok = self._send_once(topic, body, retain, wait)
        log.warning("republish %s after reconnect -> %s (%s)",
                    topic, "ok" if ok else "FAILED", self.cid)
        return ok

    def _send_once(self, topic, body, retain, wait):
        """One raw publish attempt. With wait=True, success means PUBACK
        received -- never logs 'published' without proof of delivery."""
        try:
            info = self._client.publish(topic, body, qos=QOS, retain=retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                log.warning("publish %s rejected rc=%s (%s)",
                            topic, info.rc, self.cid)
                self._connected = False
                return False
            if wait and QOS >= 1:
                try:
                    info.wait_for_publish(timeout=PUBLISH_ACK_TIMEOUT_S)
                except (ValueError, RuntimeError) as exc:
                    log.warning("publish %s not confirmed: %s (%s)",
                                topic, exc, self.cid)
                    self._connected = False
                    return False
                if not info.is_published():
                    log.warning("publish %s: no PUBACK within %ds (%s)",
                                topic, PUBLISH_ACK_TIMEOUT_S, self.cid)
                    self._connected = False
                    return False
            log.debug("published %s (retain=%s, mid=%s, %s)", topic, retain,
                      getattr(info, "mid", "?"), self.cid)
            return True
        except Exception as exc:
            log.error("publish to %s failed: %s (%s)", topic, exc, self.cid)
            self._connected = False
            return False

    # -- paho callbacks --
    def _handle_socket_open(self, client, userdata, sock):
        """Called by paho for every new (re)connect socket, including each
        auto-reconnect. Enable kernel TCP keepalive so a half-open socket from a
        WLAN<->mobile handover is detected promptly instead of after KEEPALIVE_S."""
        if client is not self._client:
            return  # stale callback from an abandoned client
        _enable_tcp_keepalive(sock)

    def _handle_connect(self, client, userdata, flags, rc):
        if client is not self._client:
            return  # stale callback from an abandoned client
        if rc == 0:
            self._connected = True
            self._conn_event.set()
            log.info("mqtt connected (%s)", self.cid)
            for topic, _kind in self._subs:
                client.subscribe(topic, qos=QOS)
                log.info("re-subscribed %s (%s)", topic, self.cid)
            if self.on_connected is not None:
                try:
                    self.on_connected()
                except Exception:
                    log.exception("on_connected callback failed (%s)", self.cid)
        else:
            self._connected = False
            log.error("mqtt connect refused rc=%s (%s)", rc, self.cid)

    def _handle_disconnect(self, client, userdata, rc):
        if client is not self._client:
            return  # stale callback from an abandoned client
        self._connected = False
        self._conn_event.clear()
        if rc == 0:
            # rc=0 means we called disconnect() ourselves; paho won't reconnect.
            log.info("mqtt disconnected cleanly (%s)", self.cid)
        else:
            log.warning("mqtt connection lost rc=%s (will auto-reconnect)", rc)

    def _handle_message(self, client, userdata, msg):
        if client is not self._client:
            return  # stale callback from an abandoned client
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            log.warning("non-JSON message on %s, ignored", topic)
            return
        device_id = _device_from_topic(topic)
        log.info("mqtt message on %s (%s)", topic, self.cid)
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
