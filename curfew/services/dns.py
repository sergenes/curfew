"""The DNS filter daemon: a small UDP resolver that answers or refuses per requesting device.

It listens on port 53 (needs root), looks up the policy for the client's IP, and either returns
NXDOMAIN for a blocked name or forwards the query to an upstream resolver and relays the reply.
It never inspects anything but the query name, and it holds no cache of its own.

Point the router's DHCP DNS at the machine running this so every device resolves through it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import signal
import struct
import time
from pathlib import Path

from curfew.config import Settings
from curfew.dnsfilter import DnsError, Policy, build_block_reply, is_blocked, parse_qname
from curfew.registry import Registry
from curfew.services.filtering import FilterService

log = logging.getLogger(__name__)

HEARTBEAT_FILE = "dns.heartbeat"
DEFAULT_UPSTREAM = "1.1.1.1"


def write_heartbeat(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / HEARTBEAT_FILE).write_text(str(time.time()))


def dns_running(data_dir: Path, *, max_age_s: float = 15.0) -> bool:
    try:
        return (time.time() - (data_dir / HEARTBEAT_FILE).stat().st_mtime) < max_age_s
    except OSError:
        return False


class _ServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_query: object) -> None:
        self._on_query = on_query
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._on_query(data, addr)  # type: ignore[operator]


class _UpstreamProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_reply: object) -> None:
        self._on_reply = on_reply
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._on_reply(data)  # type: ignore[operator]


async def run_dns(
    settings: Settings,
    *,
    upstream: str = DEFAULT_UPSTREAM,
    port: int = 53,
    listen_host: str = "0.0.0.0",
    heartbeat_s: float = 5.0,
    policy_ttl_s: float = 5.0,
) -> None:
    """Serve filtered DNS until interrupted. Needs root to bind port 53."""
    registry = Registry(settings.registry_path)
    svc = FilterService(registry)
    loop = asyncio.get_running_loop()

    pending: dict[int, tuple[bytes, tuple[str, int]]] = {}
    ids = itertools.count()
    server: _ServerProtocol | None = None
    upstream_proto: _UpstreamProtocol | None = None
    policy_cache: dict[str, tuple[float, Policy]] = {}

    def policy_for(ip: str) -> Policy:
        # Cache each client's resolved policy briefly, so we don't scan the registry on every query.
        # A new pause, block or rule takes effect within policy_ttl_s.
        now = loop.time()
        hit = policy_cache.get(ip)
        if hit is not None and hit[0] > now:
            return hit[1]
        pol = svc.policy_for_ip(ip)
        policy_cache[ip] = (now + policy_ttl_s, pol)
        return pol

    def handle_query(data: bytes, addr: tuple[str, int]) -> None:
        try:
            qname = parse_qname(data)
        except DnsError:
            return
        policy = policy_for(addr[0])
        if is_blocked(qname, policy):
            log.info("blocked %s for %s (%s)", qname, addr[0], policy.scope)
            if server is not None and server.transport is not None:
                with contextlib.suppress(OSError):
                    server.transport.sendto(build_block_reply(data), addr)
            return
        if upstream_proto is None or upstream_proto.transport is None:
            return
        new_id = next(ids) & 0xFFFF
        if len(pending) > 8192:  # guard against unanswered queries piling up
            pending.clear()
        pending[new_id] = (data[0:2], addr)
        with contextlib.suppress(OSError):
            upstream_proto.transport.sendto(struct.pack("!H", new_id) + data[2:])

    def handle_reply(data: bytes) -> None:
        if len(data) < 2 or server is None or server.transport is None:
            return
        rid = struct.unpack("!H", data[0:2])[0]
        entry = pending.pop(rid, None)
        if entry is None:
            return
        orig_id, client_addr = entry
        with contextlib.suppress(OSError):
            server.transport.sendto(orig_id + data[2:], client_addr)

    up_transport, upstream_proto = await loop.create_datagram_endpoint(
        lambda: _UpstreamProtocol(handle_reply), remote_addr=(upstream, 53)
    )
    srv_transport, server = await loop.create_datagram_endpoint(
        lambda: _ServerProtocol(handle_query), local_addr=(listen_host, port)
    )
    log.info("DNS filter on %s:%d, upstream %s", listen_host, port, upstream)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            write_heartbeat(settings.data_dir)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_s)
    finally:
        srv_transport.close()
        up_transport.close()
        registry.close()
