"""MCP server exposing the router to agents over stdio.

Run with `uv run curfew-mcp` or register it in .mcp.json (see README).
Every tool returns plain JSON so the agent can reason over it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from curfew import actions
from curfew.config import Settings
from curfew.models import Band, KnownDevice
from curfew.registry import Registry, parse_since
from curfew.scheduler import format_days, is_active
from curfew.services import status as status_service
from curfew.services.control import ControlService
from curfew.services.devices import DeviceService, humanize
from curfew.services.eclipse import EclipseService
from curfew.soap import SoapClient
from curfew.vendor import VendorDb

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
ROUTER_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

server = MCPServer(
    "curfew",
    instructions=(
        "Tools for the household NETGEAR Nighthawk CAX80 router. "
        "Devices can be referred to by MAC address, nickname, router name or IP. "
        "Nicknames and owners live in a local registry, not on the router; use name_device to set them. "
        "Randomized MACs (phones with private wifi addresses) change over time; prefer nicknames for those."
    ),
)


class _State:
    """Lazily created shared client and registry, bound to the server's event loop."""

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.client: SoapClient | None = None
        self.registry: Registry | None = None
        self.vendors: VendorDb | None = None

    def devices(self) -> DeviceService:
        if self.settings is None:
            self.settings = Settings.from_env()
        if self.client is None:
            self.client = SoapClient(self.settings)
        if self.registry is None:
            self.registry = Registry(self.settings.registry_path)
        if self.vendors is None:
            self.vendors = VendorDb(self.settings.data_dir)
        return DeviceService(self.client, self.registry, self.vendors)

    def control(self) -> ControlService:
        svc = self.devices()
        assert self.settings is not None
        return ControlService(svc.client, svc.registry, self.settings.data_dir)

    def eclipse(self) -> EclipseService:
        svc = self.devices()
        assert self.settings is not None
        return EclipseService(svc.client, svc.registry, self.settings)


state = _State()


def _model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _dump(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if isinstance(model, list):
        return [_dump(m) for m in model]
    return model


def _known(k: KnownDevice, now: datetime) -> dict[str, Any]:
    d = _model(k)
    d["display_name"] = k.display_name
    d["first_seen_ago"] = humanize(k.first_seen, now=now)
    d["last_seen_ago"] = humanize(k.last_seen, now=now)
    return d


# -- read tools -------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def router_status() -> dict[str, Any]:
    """Router model, firmware, uptime, CPU and memory, WAN state, LAN, Access Control, device count."""
    svc = state.devices()
    return _model(await status_service.get_status(svc.client))


@server.tool(annotations=READ_ONLY)
async def list_devices() -> dict[str, Any]:
    """Every device attached to the router right now, merged with nicknames, owners, tags and vendor.

    Also records the sighting in the local registry. Takes a few seconds; the router is slow here.
    """
    svc = state.devices()
    now = datetime.now(UTC)
    views = await svc.list_online()
    items = []
    for v in views:
        d = _dump(v)
        d["display_name"] = v.display_name
        d["first_seen_ago"] = humanize(v.first_seen, now=now)
        items.append(d)
    unknown = [v for v in views if not v.nickname and not v.is_known]
    return {
        "scanned_at": now.isoformat(),
        "count": len(views),
        "unnamed_count": len(unknown),
        "devices": items,
    }


@server.tool(annotations=READ_ONLY)
async def scan_network() -> dict[str, Any]:
    """Refresh the registry from the router and report what changed: new devices, returned, left."""
    svc = state.devices()
    delta = await svc.scan()
    now = delta.scanned_at
    return {
        "scanned_at": now.isoformat(),
        "present": delta.present,
        "new": [_known(k, now) for k in delta.new],
        "returned": [_known(k, now) for k in delta.returned],
        "left": [_known(k, now) for k in delta.left],
    }


@server.tool(annotations=READ_ONLY)
async def who_is_new(since: str = "7d") -> dict[str, Any]:
    """Devices first seen after `since` ('7d', '24h', '30m', or an ISO date). Runs a scan first."""
    svc = state.devices()
    await svc.scan()
    now = datetime.now(UTC)
    cutoff = parse_since(since, now=now)
    return {
        "since": cutoff.isoformat(),
        "devices": [_known(k, now) for k in svc.new_since(cutoff)],
    }


@server.tool(annotations=READ_ONLY)
async def known_devices(owner: str = "", tag: str = "", online_only: bool = False) -> dict[str, Any]:
    """Everything the registry remembers, online or not. Filter by owner, tag, or online state."""
    svc = state.devices()
    now = datetime.now(UTC)
    items = svc.registry.all()
    if owner:
        items = [k for k in items if k.owner.lower() == owner.lower()]
    if tag:
        items = [k for k in items if tag.lower() in (t.lower() for t in k.tags)]
    if online_only:
        items = [k for k in items if k.online]
    return {"count": len(items), "devices": [_known(k, now) for k in items]}


@server.tool(annotations=READ_ONLY)
async def device_history(device: str, limit: int = 20) -> dict[str, Any]:
    """Presence sessions for one device (MAC, nickname, router name or IP): when it joined and left."""
    svc = state.devices()
    now = datetime.now(UTC)
    try:
        known, sessions = svc.history(device, limit=limit)
    except LookupError as err:
        raise ToolError(str(err)) from err
    return {
        "device": _known(known, now),
        "sessions": [
            {
                **_dump(s),
                "duration_minutes": int(s.duration.total_seconds() // 60),
                "ongoing": s.ended_at is None,
            }
            for s in sessions
        ],
    }


@server.tool(annotations=READ_ONLY)
async def system_log(limit: int = 50, category: str = "") -> dict[str, Any]:
    """Recent router log entries, newest first: DHCP leases, Access Control decisions, attack warnings.

    category filters by substring, e.g. 'DHCP' or 'Access Control'.
    """
    svc = state.devices()
    entries = await actions.get_system_logs(svc.client)
    if category:
        entries = [e for e in entries if category.lower() in e.category.lower()]
    return {"entries": _dump(entries[:limit])}


@server.tool(annotations=READ_ONLY)
async def wifi_info() -> dict[str, Any]:
    """SSIDs, channels, security mode and guest network state for the 2.4 GHz and 5 GHz bands."""
    svc = state.devices()
    return {"networks": _dump(await status_service.get_wifi(svc.client))}


@server.tool(annotations=READ_ONLY)
async def traffic_stats() -> dict[str, Any]:
    """Traffic meter totals for today, yesterday, week, month. Zero unless the meter is enabled."""
    svc = state.devices()
    return _model(await actions.get_traffic_stats(svc.client))


@server.tool(annotations=READ_ONLY)
async def check_firmware() -> dict[str, Any]:
    """Ask the router whether a firmware update is available. Slow: about 10 seconds."""
    svc = state.devices()
    f = await actions.check_firmware(svc.client)
    return {**_model(f), "update_available": f.update_available}


# -- local write tools --------------------------------------------------------


@server.tool(annotations=LOCAL_WRITE)
async def name_device(
    device: str,
    nickname: str | None = None,
    owner: str | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Assign a nickname, owner, tags or notes in the local registry (nothing changes on the router).

    device: MAC, current nickname, router name or IP. Pass "" to clear a field; omit to leave it alone.
    """
    svc = state.devices()
    try:
        known = svc.name(device, nickname=nickname, owner=owner, tags=tags, notes=notes)
    except (LookupError, ValueError) as err:
        raise ToolError(str(err)) from err
    return _known(known, datetime.now(UTC))


# -- groups and access ----------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def list_groups() -> dict[str, Any]:
    """Device groups (kids, parents, iot, ...) with their members and current access state."""
    svc = state.devices()
    now = datetime.now(UTC)
    return {
        "groups": {name: [_known(k, now) for k in members] for name, members in svc.registry.groups().items()}
    }


@server.tool(annotations=LOCAL_WRITE)
async def set_group_membership(device: str, group: str, member: bool = True) -> dict[str, Any]:
    """Add a device to a group (member=true) or remove it (member=false). Groups are created on first use."""
    svc = state.devices()
    try:
        mac = svc.resolve(device).mac
        known = svc.registry.add_tags(mac, [group]) if member else svc.registry.remove_tags(mac, [group])
    except (LookupError, ValueError) as err:
        raise ToolError(str(err)) from err
    return _known(known, datetime.now(UTC))


@server.tool(annotations=ROUTER_WRITE)
async def set_access(target: str, enabled: bool, reason: str = "") -> dict[str, Any]:
    """Turn internet access on or off for a group (e.g. "kids"), an owner (e.g. "Sam") or one device.

    Blocking uses the router's Access Control; it is enabled automatically the first time.
    A manual change lasts until the target's next scheduled change, or until flipped back if it has none.
    """
    ctl = state.control()
    try:
        change = await ctl.set_access(target, allow=enabled, reason=reason)
    except (LookupError, ValueError) as err:
        raise ToolError(str(err)) from err
    now = datetime.now(UTC)
    return {
        "target": change.target,
        "kind": change.kind,
        "access": "on" if change.allow else "off",
        "devices": [_known(k, now) for k in change.devices],
        "until": change.until.isoformat() if change.until else None,
        "access_control_enabled_now": change.access_control_enabled_now,
    }


@server.tool(annotations=READ_ONLY)
async def access_status() -> dict[str, Any]:
    """Whether Access Control is on, which devices are blocked and why, and which schedules are active."""
    ctl = state.control()
    enabled = await actions.get_access_control_enabled(ctl.client)
    await ctl.devices.scan()
    now = datetime.now(UTC)
    overrides = ctl.registry.overrides()
    rules = ctl.registry.rules()
    blocked = []
    for k in ctl.registry.all():
        if k.access_state != "block":
            continue
        o = overrides.get(k.mac)
        blocked.append(
            {
                **_known(k, now),
                "reason": "manual" if o else "schedule",
                "until": o.until.isoformat() if o and o.until else None,
            }
        )
    return {
        "access_control_enabled": enabled,
        "blocked": blocked,
        "active_schedules": [r.name for r in rules if is_active(r, now.astimezone())],
    }


# -- schedules -------------------------------------------------------------------


@server.tool(annotations=LOCAL_WRITE)
async def add_schedule(
    target: str, start: str, end: str, days: str = "daily", name: str = ""
) -> dict[str, Any]:
    """Block a group, owner or device every day between start and end (local time, e.g. 21:00 to 07:00).

    days: daily, weekdays, weekends, or a list like mon-fri or sat,sun.
    Enforced by the background watcher or by apply_schedules.
    """
    ctl = state.control()
    try:
        rule = ctl.add_schedule(target, start=start, end=end, days=days, name=name)
    except (LookupError, ValueError) as err:
        raise ToolError(str(err)) from err
    return {**_model(rule), "days_text": format_days(rule.days)}


@server.tool(annotations=READ_ONLY)
async def list_schedules() -> dict[str, Any]:
    """All schedules with whether each is active right now."""
    ctl = state.control()
    now_local = datetime.now().astimezone()
    return {
        "schedules": [
            {**_model(r), "days_text": format_days(r.days), "active_now": is_active(r, now_local)}
            for r in ctl.registry.rules()
        ]
    }


@server.tool(annotations=LOCAL_WRITE)
async def remove_schedule(schedule_id: int) -> dict[str, Any]:
    """Delete a schedule by id. Devices it blocked are released at the next apply."""
    ctl = state.control()
    return {"removed": ctl.remove_schedule(schedule_id)}


@server.tool(annotations=ROUTER_WRITE)
async def apply_schedules() -> dict[str, Any]:
    """Evaluate schedules and manual overrides now and push any needed allow/block changes to the router."""
    ctl = state.control()
    await ctl.devices.scan()
    changes = await ctl.apply_schedules()
    now = datetime.now(UTC)
    return {"changes": [{**_known(k, now), "access": "on" if allow else "off"} for k, allow in changes]}


# -- router writes ----------------------------------------------------------------


@server.tool(annotations=DESTRUCTIVE)
async def reboot_router(confirm: bool = False) -> dict[str, Any]:
    """Reboot the router. Everyone loses internet for about two minutes. Requires confirm=true."""
    if not confirm:
        raise ToolError("refusing to reboot without confirm=true; ask the user first")
    ctl = state.control()
    await ctl.reboot()
    return {"rebooting": True}


@server.tool(annotations=ROUTER_WRITE)
async def set_guest_wifi(enabled: bool, band: str = "both") -> dict[str, Any]:
    """Turn the guest wifi network on or off. band: 2.4, 5, or both."""
    bands = {"2.4": [Band.GHZ_2_4], "5": [Band.GHZ_5], "both": [Band.GHZ_2_4, Band.GHZ_5]}.get(band)
    if bands is None:
        raise ToolError("band must be 2.4, 5 or both")
    ctl = state.control()
    for b in bands:
        await ctl.set_guest_wifi(b, enabled)
    return {"guest_wifi": "on" if enabled else "off", "bands": [b.value for b in bands]}


# -- eclipse pause (instant ARP cutoff) -----------------------------------------


@server.tool(annotations=ROUTER_WRITE)
async def pause_device(target: str, reason: str = "") -> dict[str, Any]:
    """Instantly cut a group, owner or device off the internet via Eclipse Pause (ARP interception).

    This is the immediate cutoff that works on already-connected devices, unlike the MAC block.
    The device stays on wifi and reachable on the LAN. Records the pause; the eclipse daemon
    (running as root) enforces it within a couple of seconds. Use resume_device to lift it.
    """
    ecl = state.eclipse()
    try:
        _kind, members = _resolve_members(ecl, target)
    except (LookupError, ValueError) as err:
        raise ToolError(str(err)) from err
    await ecl.devices.scan()
    paused = []
    for m in members:
        fresh = ecl.registry.get(m.mac) or m
        if not fresh.last_ip:
            continue
        ecl.registry.set_pause(m.mac, fresh.last_ip, reason)
        paused.append(fresh)
    now = datetime.now(UTC)
    return {
        "target": target,
        "kind": _kind,
        "paused": [_known(k, now) for k in paused],
        "enforcing": ecl.enforcing(),
        "note": ""
        if ecl.enforcing()
        else "the eclipse daemon is not running, so this is recorded but not enforced yet",
    }


@server.tool(annotations=ROUTER_WRITE)
async def resume_device(target: str = "", all_devices: bool = False) -> dict[str, Any]:
    """Lift an Eclipse Pause for a group, owner or device, or all_devices=true for everyone."""
    ecl = state.eclipse()
    if all_devices:
        return {"resumed": ecl.resume_all()}
    if not target:
        raise ToolError("name a target or pass all_devices=true")
    try:
        _kind, members = _resolve_members(ecl, target)
    except (LookupError, ValueError) as err:
        raise ToolError(str(err)) from err
    resumed = sum(1 for m in members if ecl.registry.clear_pause(m.mac))
    return {"resumed": resumed, "enforcing": ecl.enforcing()}


@server.tool(annotations=READ_ONLY)
async def list_paused() -> dict[str, Any]:
    """Devices currently held in Eclipse Pause, and whether the enforcement daemon is running."""
    ecl = state.eclipse()
    return {"enforcing": ecl.enforcing(), "paused": [_model(p) for p in ecl.list_paused()]}


def _resolve_members(ecl: EclipseService, target: str) -> tuple[str, list[Any]]:
    """Group -> owner -> single device, mirroring ControlService.resolve_target."""
    groups = ecl.registry.groups()
    for name, members in groups.items():
        if name.lower() == target.strip().lower():
            return "group", members
    owned = ecl.registry.by_owner(target)
    if owned:
        return "owner", owned
    return "device", [ecl.devices.resolve(target)]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
