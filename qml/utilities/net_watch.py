#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""net_watch.py -- network handover detection via the preferred source address.

A WLAN<->mobile handover changes the source address the kernel would pick for
the broker. If that differs from the source address of the live MQTT socket,
the socket is stranded and the client must reconnect. Works without D-Bus,
netlink or any Sailjail permission: connect() on a UDP socket sends no packets,
it is only a route lookup.

Used by the UI process (api.py) for its persistent -ui client; the GPS daemon
publishes connect-per-tick and has no standing connection to check, and the
-cmd daemon listens to the ConnMan state signal instead (see daemon_cmd.py).

No timers here -- callers invoke the check only at points where the process is
awake anyway (battery guideline).
"""

import logging
import socket
import time

log = logging.getLogger("fmd.netwatch")

# The broker host is usually a DNS name; never resolve on every call (a
# resolver round trip is a radio wakeup of its own). 5 min TTL.
_DNS_TTL_S = 300
_dns_cache = {"host": None, "addr": None, "family": None, "ts": 0.0}


def _resolve(host, port):
    """Resolve host to (address, family) with a small TTL cache. Prefers IPv4;
    returns (None, None) on resolution failure."""
    now = time.time()
    if _dns_cache["host"] == host and now - _dns_cache["ts"] < _DNS_TTL_S:
        return _dns_cache["addr"], _dns_cache["family"]
    try:
        infos = socket.getaddrinfo(host, int(port), 0, socket.SOCK_DGRAM)
    except OSError as exc:
        log.debug("resolve %s failed: %s", host, exc)
        return None, None
    if not infos:
        return None, None
    # Prefer IPv4: mixed v4/v6 setups would otherwise compare a v6 source
    # address against a v4 socket and report a bogus handover.
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    family, _t, _p, _c, sockaddr = infos[0]
    _dns_cache.update(host=host, addr=sockaddr[0], family=family, ts=now)
    return sockaddr[0], family


def preferred_src_ip(host, port):
    """Source address the kernel would choose for host:port, or None.

    None means "could not determine" (DNS failure, no route) and must be
    treated as "skip the check", never as a detected handover."""
    addr, family = _resolve(host, port)
    if addr is None:
        return None
    try:
        s = socket.socket(family, socket.SOCK_DGRAM)
        try:
            s.connect((addr, int(port)))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
