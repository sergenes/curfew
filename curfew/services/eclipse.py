"""Eclipse Pause service: record which devices are paused, and the daemon that enforces it.

Recording a pause is cheap and needs no privileges: it writes a row to the registry.
Enforcing it (sending ARP frames continuously) needs root and runs in the `eclipse` daemon.
The two talk only through the registry, so `pause` from the CLI or an agent takes effect on
the daemon's next loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

from curfew.config import Settings
from curfew.models import PauseTarget
from curfew.pause import (
    PauseEngine,
    PauseError,
    ScapySender,
    Target,
    build_network_info,
    diff_targets,
    eclipse_running,
    write_heartbeat,
)
from curfew.registry import Registry
from curfew.services.devices import DeviceService
from curfew.soap import SoapClient

log = logging.getLogger(__name__)


class EclipseService:
    """Read/write the paused set. No raw sockets here; enforcement is the daemon's job."""

    def __init__(self, client: SoapClient, registry: Registry, settings: Settings) -> None:
        self.client = client
        self.registry = registry
        self.settings = settings
        self.devices = DeviceService(client, registry)

    async def pause(self, device: str, reason: str = "") -> PauseTarget:
        known = self.devices.resolve(device)
        await self.devices.scan()  # refresh the device's current IP and presence
        fresh = self.registry.get(known.mac) or known
        ip = fresh.last_ip
        if not ip:
            raise PauseError(f"{fresh.display_name} has no current IP; is it online?")
        if ip == self.settings.host:
            raise PauseError("refusing to pause the router")
        self.registry.set_pause(known.mac, ip, reason)
        return PauseTarget(
            mac=known.mac, ip=ip, since=datetime.now(UTC), reason=reason, display_name=fresh.display_name
        )

    def resume(self, device: str) -> bool:
        known = self.devices.resolve(device)
        return self.registry.clear_pause(known.mac)

    def resume_all(self) -> int:
        return self.registry.clear_all_pauses()

    def list_paused(self) -> list[PauseTarget]:
        return self.registry.pauses()

    def enforcing(self) -> bool:
        return eclipse_running(self.settings.data_dir)


async def run_daemon(
    settings: Settings,
    *,
    interval: float = 2.0,
    refresh_s: float = 60.0,
    iface: str | None = None,
) -> None:
    """Continuously enforce the paused set with ARP. Needs root. Heals everything on exit."""
    net = build_network_info(settings.host, iface)
    log.info(
        "Eclipse on %s (us %s / %s, router %s / %s)",
        net.iface,
        net.our_ip,
        net.our_mac,
        net.gateway_ip,
        net.gateway_mac,
    )
    engine = PauseEngine(net, ScapySender(net.iface))
    registry = Registry(settings.registry_path)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    active: dict[str, Target] = {}
    last_refresh = 0.0
    try:
        while not stop.is_set():
            now = loop.time()
            if now - last_refresh >= refresh_s:
                await _refresh_ips(settings, registry)
                last_refresh = now

            current = {p.mac: Target(mac=p.mac, ip=p.ip) for p in registry.pauses() if p.ip}
            added, removed = diff_targets(active, current)
            for t in added:
                log.info("pausing %s (%s)", t.mac, t.ip)
            for t in removed:
                log.info("resuming %s (%s)", t.mac, t.ip)
                engine.heal(t)
            active = current

            for t in active.values():
                try:
                    engine.spoof(t)
                except PauseError as err:
                    log.warning("skipping %s: %s", t.mac, err)
            write_heartbeat(settings.data_dir)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
    finally:
        log.info("healing %d paused device(s) before exit", len(active))
        for t in active.values():
            engine.heal(t)
        engine.close()
        registry.close()


async def _refresh_ips(settings: Settings, registry: Registry) -> None:
    """Ask the router who is online, so paused devices keep the right IP across DHCP changes."""
    try:
        async with SoapClient(settings) as client:
            devices = DeviceService(client, registry)
            await devices.scan()
    except Exception as err:  # noqa: BLE001 - a scan failure must not stop enforcement
        log.debug("IP refresh skipped: %s", err)
        return
    for p in registry.pauses():
        fresh = registry.get(p.mac)
        if fresh and fresh.last_ip and fresh.last_ip != p.ip:
            log.info("%s moved %s -> %s", p.mac, p.ip, fresh.last_ip)
            registry.update_pause_ip(p.mac, fresh.last_ip)
