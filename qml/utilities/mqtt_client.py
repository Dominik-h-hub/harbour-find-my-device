#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mqtt_client.py -- MQTT wrapper for harbour-find-my-device.

Responsibilities:
  * Client ids:  <device-id>-pub / <device-id>-cmd / <device-id>-ui
  * Topics:      fmd/<id>            location, RETAIN=true,  QoS 1
                 fmd/<id>/cmd        commands,  RETAIN=false, QoS 1
                 fmd/<id>/cmd/ack    acks,      RETAIN=false, QoS 1
                 fmd/<id>/hc         health check, RETAIN=false, QoS 1
                                     (published AND subscribed by the same
                                      client -- the broker echoes it back)
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
import time

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

# The republish after a forced reconnect writes into a socket whose TLS
# handshake just finished on a network that is still settling from a handover,
# so the broker's first PUBACK is measurably slower than on a warm link. This
# is the attempt that decides between "delivered" and "lost" -- give it room.
PUBLISH_ACK_RETRY_TIMEOUT_S = 10

# CONNACK wait after connect()/force_reconnect(). 25s, not 15: a TLS connect on
# a freshly handed-over network regularly needs longer than 15s (measured 17.3s
# on the UI client, and two 15s timeouts in a row in the GPS publisher -- each
# one threw away a payload that the link would have carried a second later).
CONNACK_TIMEOUT_S = 25

# OS-level TCP keepalive. The MQTT keepalive above is deliberately long, but a
# WLAN<->mobile handover leaves the old socket "half-open".
#
# KEEPIDLE is also what keeps the path itself alive between publishes. At 300s
# it did not: a 168min field log showed 13 connection losses, and the surviving
# connection lifetimes clustered on multiples of the 5.5min GPS interval
# (7x ~350s, 3x ~700s) -- i.e. the link died during the FIRST idle gap and the
# next publish only surfaced it. 12 of 22 positions then had to go out through
# the reconnect+republish repair, each costing a full TLS handshake. 120s sits
# under the 3-6min NAT mapping lifetime typical for mobile carriers, so the
# mapping is refreshed before it expires. Do NOT lower KEEPALIVE_S instead:
# TCP keepalive is handled by the kernel and therefore survives the Sailfish
# suspend that freezes the paho thread, an MQTT PINGREQ does not.
TCP_KEEPIDLE_S = 120     # start probing after 2 min idle
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

# Application-level liveness probe: publish to fmd/<id>/hc while subscribed to
# that same topic, so the broker echoes every beat straight back.
#
# This is the only check that exercises the COMPLETE loop -- socket out, broker,
# subscription routing, socket in, callback. Everything else this module can
# ask is a client-side belief: paho reports "connected" from a local state
# variable that survives a dead peer, and a broker session that has quietly
# stopped routing to us is indistinguishable from a quiet topic.
HEALTHCHECK_INTERVAL_S = 60

# How long heartbeats may go unanswered before the channel counts as defective.
HEALTHCHECK_TIMEOUT_S = 45


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


def topic_health(device_id):
    return "fmd/%s/hc" % device_id


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
        # Requested subscriptions (topic -> kind) and the ones the broker has
        # actually acknowledged on the CURRENT connection. Tracked separately
        # because subscribe() only queues a packet: it returns long before
        # anything reaches the broker, so "we asked" and "we receive" are
        # genuinely different facts. _subs_pending maps the SUBSCRIBE mid to
        # its topic until the SUBACK arrives.
        self._subs = {}
        self._subs_confirmed = set()
        self._subs_pending = {}
        # Guards the three above: _add_sub writes from the caller thread while
        # _handle_connect/_handle_subscribe read and write from the paho
        # network thread. Iterating _subs unlocked could raise "Set changed
        # size during iteration" inside on_connect, which paho re-raises --
        # killing the network thread at connect time.
        self._subs_lock = threading.Lock()
        self._clean_session = clean_session
        # Set on CONNACK rc=0, cleared on connect()/disconnect; wait_connected()
        # blocks on it (connect() is async: connect_async + loop_start).
        self._conn_event = threading.Event()
        # Serialises force_reconnect() so concurrent repair paths (publish
        # failure, ConnMan signal, net watch) never tear down the same client
        # twice in parallel.
        self._reconnect_lock = threading.Lock()
        # Heartbeat (off unless start_healthcheck() is called).
        # _hc_unanswered_since is the send time of the OLDEST beat that has not
        # been echoed yet, 0.0 when everything sent has come back. It has to be
        # the oldest, not the newest: measuring against the newest beat would
        # reset the clock every interval, so with an interval below the timeout
        # the check could never fire at all, and with one just above it the
        # defect would flicker in and out faster than the watchdog can confirm
        # it. Clearing it on the echo -- rather than comparing timestamps to
        # wall-clock -- is also what lets this survive a suspend, when neither
        # thread runs for minutes at a time.
        self._hc_topic = None
        self._hc_interval = 0
        self._hc_timeout = HEALTHCHECK_TIMEOUT_S
        self._hc_unanswered_since = 0.0
        self._hc_last_echo = 0.0
        self._hc_stop = threading.Event()
        self._hc_thread = None

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
            self._client.suppress_exceptions = True
            self._client.on_connect = self._handle_connect
            self._client.on_disconnect = self._handle_disconnect
            self._client.on_message = self._handle_message
            self._client.on_subscribe = self._handle_subscribe
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
        # Confirmations belong to the connection that is going away; the next
        # one has to earn its own SUBACKs. _subs (the wish list) is kept.
        with self._subs_lock:
            self._subs_confirmed.clear()
            self._subs_pending.clear()
        if old is not None:
            # Unbind the callbacks first: an abandoned network thread must
            # never touch this wrapper again. A late on_disconnect from an old
            # client used to mark the freshly connected successor as offline
            # (see also the staleness guards in the _handle_* callbacks).
            try:
                old.on_connect = None
                old.on_disconnect = None
                old.on_message = None
                old.on_subscribe = None
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
        self.stop_healthcheck()
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

    # -- health check --
    def start_healthcheck(self, device_id, interval=HEALTHCHECK_INTERVAL_S,
                          timeout=HEALTHCHECK_TIMEOUT_S):
        """Subscribe to fmd/<id>/hc and start beating on it.

        Survives reconnects on purpose: the thread only probes while the client
        reports a connection and stops for good on close(), so force_reconnect()
        does not have to tear it down and rebuild it."""
        if self._hc_thread is not None:
            return
        self._hc_topic = topic_health(device_id)
        self._hc_interval = int(interval)
        self._hc_timeout = int(timeout)
        self._hc_unanswered_since = 0.0
        self._hc_last_echo = 0.0
        self._add_sub(self._hc_topic, "hc")
        self._hc_stop.clear()
        self._hc_thread = threading.Thread(target=self._healthcheck_loop,
                                           daemon=True,
                                           name="fmd-mqtt-healthcheck")
        self._hc_thread.start()
        log.info("health check every %ds on %s (stale after %ds, %s)",
                 self._hc_interval, self._hc_topic, self._hc_timeout, self.cid)

    def stop_healthcheck(self):
        self._hc_stop.set()
        thread = self._hc_thread
        self._hc_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def _healthcheck_loop(self):
        """Publish one beat per interval. Never waits for the PUBACK: the echo
        is the proof we are after, and a verified publish would block this
        thread for up to ~42s and start a reconnect of its own, racing the one
        the watchdog is about to make."""
        while not self._hc_stop.wait(self._hc_interval):
            if self._closed:
                return
            if not self.is_really_connected():
                continue  # nothing to probe; health_defect() reports that
            sent = self._publish(self._hc_topic, {"t": int(time.time())},
                                 retain=False, wait=False)
            # Only the first beat of an unanswered run starts the clock.
            if sent and self._hc_unanswered_since == 0.0:
                self._hc_unanswered_since = time.time()

    def healthcheck_stale(self):
        """True when beats have gone unanswered for too long.

        The clock runs from the oldest unanswered beat and is cleared by the
        echo, so a suspend -- in which neither the beat thread nor the watchdog
        runs -- cannot fake a defect: the echo is read on wake and clears it."""
        if not self._hc_interval:
            return False
        since = self._hc_unanswered_since
        if since <= 0.0:
            return False  # everything sent has been echoed back
        return (time.time() - since) > self._hc_timeout

    def network_thread_alive(self):
        """True while paho's network thread is running.

        That thread is the only thing that reads the socket, answers PINGREQ
        and drives reconnects. If it dies, nothing else notices: paho leaves
        _state at mqtt_cs_connected, so is_connected() -- and therefore
        is_really_connected() -- keep returning True on a client that is deaf
        and mute. Health checks have to ask this too."""
        client = self._client
        if client is None:
            return False
        thread = getattr(client, "_thread", None)
        return bool(thread is not None and thread.is_alive())

    def subscriptions_ok(self):
        """True when the broker acknowledged every requested subscription.

        This is the check a connection test cannot make. A broker can hold a
        perfectly healthy TCP connection whose session carries no subscription
        at all (SUBSCRIBE lost or refused): the socket stays ESTABLISHED, paho
        stays connected, nothing is ever delivered, and MQTT has no mechanism
        that would surface it. Only the SUBACK does."""
        with self._subs_lock:
            return not (set(self._subs) - self._subs_confirmed)

    def health_defect(self):
        """Short reason string if this client needs repair, else None.

        Ordered most to least fatal so callers can pick a grace period per
        reason: a dead thread never recovers by itself, a missing connection
        is paho's own auto-reconnect job, and an unconfirmed subscription sits
        somewhere in between."""
        if self._client is None:
            return "no client"
        if not self.network_thread_alive():
            return "network thread dead"
        if not self.is_really_connected():
            return "disconnected"
        if not self.subscriptions_ok():
            return "subscription unconfirmed"
        if self.healthcheck_stale():
            return "healthcheck stale"
        return None

    def wait_connected(self, timeout=CONNACK_TIMEOUT_S):
        """Block until the CONNACK arrived (connect() is asynchronous).

        A publish right after connect() would otherwise always fail. Single
        event wait, no polling. Returns bool."""
        return self._conn_event.wait(timeout)

    def _refresh_connected(self):
        """Re-derive the wrapper flag from paho after a failed publish.

        NEVER latch it to False here. A missing PUBACK says nothing about the
        session state -- the packet may simply be late. Latching was effectively
        permanent: only _handle_connect sets the flag back and that does not
        fire while paho stays connected, so a single unconfirmed publish locked
        this client out of *sending* for minutes while it kept happily receiving
        on the very same socket."""
        self._connected = bool(self._client is not None
                               and self._client.is_connected())

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
        with self._subs_lock:
            self._subs[topic] = kind
        if self._client is not None and self._connected:
            self._subscribe_now(self._client, topic, kind)
        # Not connected yet is fine: _handle_connect sends every remembered
        # topic on CONNACK, so callers may register subscriptions before the
        # connection is up.

    def _subscribe_now(self, client, topic, kind):
        """Send one SUBSCRIBE and remember its mid until the SUBACK arrives.

        Deliberately does NOT log success: subscribe() returns as soon as the
        packet sits in paho's out-queue, before a single byte has reached the
        socket, let alone the broker. Announcing a subscription here claims
        something that may never become true -- and a client that believes it
        is subscribed while the broker disagrees receives nothing, forever,
        with every connection check still reporting green. Only
        _handle_subscribe may confirm.

        The mid must be recorded under the SAME _subs_lock hold that queues the
        packet. subscribe() returns once the SUBSCRIBE sits in paho's out-queue,
        and the network thread can have written it and read the SUBACK back
        before this thread gets to run again: registering the mid afterwards
        loses that race, _handle_subscribe discards the SUBACK as an unknown
        mid, and the topic stays unconfirmed forever -- subscriptions_ok() then
        reports a permanent defect and the health check reconnects in a loop,
        re-losing the race on every new connection. Holding the lock across both
        steps makes the callback wait instead of miss. Safe in threaded mode
        (loop_start): paho's subscribe() path only appends to the out-queue and
        nudges the sockpair, it never blocks on a lock the network thread holds
        while that thread waits for _subs_lock."""
        with self._subs_lock:
            try:
                rc, mid = client.subscribe(topic, qos=QOS)
            except Exception as exc:
                log.error("subscribe %s failed: %s (%s)", topic, exc, self.cid)
                return
            if rc != mqtt.MQTT_ERR_SUCCESS:
                log.error("subscribe %s not queued rc=%s (%s)",
                          topic, rc, self.cid)
                return
            self._subs_pending[mid] = topic
        log.info("subscribe %s sent (%s, mid=%s, %s)", topic, kind, mid, self.cid)

    # -- publishing --
    def publish_location(self, device_id, payload, wait=True):
        """Publish a location payload (retain=true, QoS1)."""
        return self._publish(topic_location(device_id), payload, retain=True,
                             wait=wait)

    def publish_command(self, device_id, payload, wait=True):
        """Publish a command to a remote device (retain=false, QoS1)."""
        return self._publish(topic_cmd(device_id), payload, retain=False,
                             wait=wait)

    def publish_ack(self, device_id, payload):
        """Publish a command result on the ack topic (retain=false, QoS1)."""
        return self._publish(topic_ack(device_id), payload, retain=False)

    def _publish(self, topic, payload, retain, wait=True):
        """Publish with delivery verification and one self-repair attempt.

        wait=True (default): block until the QoS1 PUBACK arrived; on failure
        force a hard reconnect and republish the SAME payload exactly once
        (a reconnect alone would save the connection but lose this tick's
        position). Worst case ~42s (5s PUBACK + ~2s teardown + 25s CONNACK +
        10s PUBACK), so wait=True callers MUST run on a thread nobody waits on
        -- never on the PyOtherSide worker and never on a UI path.

        wait=False MUST be used by any caller running in the paho network
        thread (e.g. an on_connected flush): waiting for the PUBACK there
        blocks exactly the thread that would process it, guaranteeing the
        timeout. The wait=False path also skips the reconnect/republish repair.

        The caller must also not hold a lock that any paho callback needs:
        force_reconnect() below runs the new client's on_connected on the paho
        network thread, and parking that thread means the PUBACK the republish
        waits for is never processed -- the repair then fails by construction.
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
            return self._send_once(topic, body, retain, wait,
                                   timeout=PUBLISH_ACK_RETRY_TIMEOUT_S)
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
        ok = self._send_once(topic, body, retain, wait,
                             timeout=PUBLISH_ACK_RETRY_TIMEOUT_S)
        log.warning("republish %s after reconnect -> %s (%s)",
                    topic, "ok" if ok else "FAILED", self.cid)
        return ok

    def _send_once(self, topic, body, retain, wait, timeout=None):
        """One raw publish attempt. With wait=True, success means PUBACK
        received -- never logs 'published' without proof of delivery."""
        if timeout is None:
            timeout = PUBLISH_ACK_TIMEOUT_S
        try:
            info = self._client.publish(topic, body, qos=QOS, retain=retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                log.warning("publish %s rejected rc=%s (%s)",
                            topic, info.rc, self.cid)
                self._refresh_connected()
                return False
            if wait and QOS >= 1:
                try:
                    info.wait_for_publish(timeout=timeout)
                except (ValueError, RuntimeError) as exc:
                    log.warning("publish %s not confirmed: %s (%s)",
                                topic, exc, self.cid)
                    self._refresh_connected()
                    return False
                if not info.is_published():
                    log.warning("publish %s: no PUBACK within %ds (%s)",
                                topic, timeout, self.cid)
                    self._refresh_connected()
                    return False
            log.debug("published %s (retain=%s, mid=%s, %s)", topic, retain,
                      getattr(info, "mid", "?"), self.cid)
            return True
        except Exception as exc:
            log.error("publish to %s failed: %s (%s)", topic, exc, self.cid)
            self._refresh_connected()
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
            # A CONNACK means a new broker session. With clean_session it
            # carries no subscriptions, and the previous SUBACKs say nothing
            # about it, so every topic has to be requested and confirmed
            # again. Snapshot under the lock: this runs on the paho network
            # thread while _add_sub may be writing from the caller's.
            with self._subs_lock:
                self._subs_confirmed.clear()
                self._subs_pending.clear()
                topics = list(self._subs.items())
            for topic, kind in topics:
                self._subscribe_now(client, topic, kind)
            # A beat sent on the previous connection proves nothing about this
            # one, and leaving it in place would report the fresh session as
            # stale immediately.
            self._hc_unanswered_since = 0.0
            self._hc_last_echo = 0.0
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
        with self._subs_lock:
            self._subs_confirmed.clear()
            self._subs_pending.clear()
        if rc == 0:
            # rc=0 means we called disconnect() ourselves; paho won't reconnect.
            log.info("mqtt disconnected cleanly (%s)", self.cid)
        else:
            log.warning("mqtt connection lost rc=%s (will auto-reconnect)", rc)

    def _handle_subscribe(self, client, userdata, mid, granted_qos, *_v5):
        """SUBACK -- the only proof that the broker accepted a subscription.

        Also the only place a refusal becomes visible: a broker that rejects a
        topic answers 0x80 and then simply never delivers, which from the
        client side is indistinguishable from a quiet topic. The trailing *_v5
        swallows the properties argument the MQTT 5 signature adds."""
        if client is not self._client:
            return  # stale callback from an abandoned client
        with self._subs_lock:
            topic = self._subs_pending.pop(mid, None)
        # MQTT 3.1.1 hands us plain ints, MQTT 5 ReasonCodes objects.
        codes = [int(getattr(q, "value", q)) for q in (granted_qos or [])]
        if topic is None:
            log.warning("SUBACK for unknown mid=%s rc=%s (%s)",
                        mid, codes, self.cid)
            return
        if not codes or any(c >= 0x80 for c in codes):
            log.error("broker REFUSED subscription %s rc=%s (%s)",
                      topic, codes, self.cid)
            return
        with self._subs_lock:
            self._subs_confirmed.add(topic)
        log.info("subscription confirmed %s (qos=%s, %s)",
                 topic, codes, self.cid)

    def _handle_message(self, client, userdata, msg):
        if client is not self._client:
            return  # stale callback from an abandoned client
        topic = msg.topic
        if topic.endswith("/hc"):
            # Routed by suffix like /cmd and /cmd/ack, so a beat can never
            # reach a payload handler. That matters for the clients which do
            # NOT run a health check: the UI's on_location would store {"t":
            # ...} as a fix with lat/lon NULL and refresh the map from it.
            # Unreachable today (nobody subscribes to a foreign /hc), but the
            # suffix check keeps it that way if a wildcard is ever added.
            #
            # Only OUR OWN beat clears the staleness clock -- another device's
            # says nothing about our link. Handled before parsing and before
            # the info log below: this arrives every HEALTHCHECK_INTERVAL_S and
            # would otherwise flood the rotating file log.
            if topic == self._hc_topic:
                self._hc_last_echo = time.time()
                self._hc_unanswered_since = 0.0
                log.debug("health check echo (%s)", self.cid)
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            log.warning("non-JSON message on %s, ignored", topic)
            return
        if not isinstance(payload, dict):
            # Valid JSON that is not an object (123, "RING", [...]) would hit
            # payload.get() in the handlers below. Anyone can publish to these
            # topics, so treat the shape as untrusted input, not as our bug.
            log.warning("message on %s is not a JSON object (%s), ignored",
                        topic, type(payload).__name__)
            return
        device_id = _device_from_topic(topic)
        log.info("mqtt message on %s (%s)", topic, self.cid)
        try:
            if topic.endswith("/cmd/ack"):
                if self.on_ack:
                    self.on_ack(device_id, payload)
            elif topic.endswith("/cmd"):
                if self.on_command:
                    self.on_command(device_id, payload)
            else:
                if self.on_location:
                    self.on_location(device_id, payload)
        except Exception:
            # Belt to suppress_exceptions' braces, and the only place the
            # traceback reaches the file log instead of just stderr.
            log.exception("message handler for %s failed (%s)", topic, self.cid)


def _device_from_topic(topic):
    """Extract <device-id> from fmd/<id>[/cmd[/ack]]."""
    parts = topic.split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "fmd" else None
