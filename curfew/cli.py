"""Command line interface. `curfew --help` lists the commands."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import typer
from pydantic import BaseModel
from rich import box
from rich.console import Console
from rich.table import Table

from curfew import actions
from curfew.config import MissingCredentials, Settings
from curfew.models import AccessOverride, Band, DeviceView, KnownDevice, Rule
from curfew.pause import PauseError, build_network_info, eclipse_running
from curfew.registry import Registry, parse_since
from curfew.scheduler import format_days, is_active
from curfew.services import status as status_service
from curfew.services.control import ControlService, Enforcement
from curfew.services.devices import DeviceService, humanize
from curfew.services.dns import DEFAULT_UPSTREAM, dns_running, run_dns
from curfew.services.eclipse import EclipseService, run_daemon
from curfew.services.filtering import FilterService
from curfew.soap import SoapClient, SoapError
from curfew.vendor import VendorDb

app = typer.Typer(help="Control a NETGEAR Nighthawk CAX80 from the command line.", no_args_is_help=True)
watcher_app = typer.Typer(help="Manage the background watcher (macOS launchd).", no_args_is_help=True)
vendors_app = typer.Typer(help="MAC vendor table.", no_args_is_help=True)
group_app = typer.Typer(
    help="Groups of devices (stored as tags), e.g. kids, parents, iot.", no_args_is_help=True
)
access_app = typer.Typer(
    help="Turn internet access on or off for a device, owner or group.", no_args_is_help=True
)
schedule_app = typer.Typer(help="Recurring access windows, e.g. bedtime.", no_args_is_help=True)
guest_app = typer.Typer(help="Guest wifi.", no_args_is_help=True)
eclipse_app = typer.Typer(
    help="Eclipse Pause: instant ARP-based internet cutoff (needs root).", no_args_is_help=True
)
filter_app = typer.Typer(
    help="Per-group/device website allow and deny lists, enforced by the DNS filter.", no_args_is_help=True
)
dns_app = typer.Typer(help="The DNS filter daemon (needs root) and its status.", no_args_is_help=True)
app.add_typer(watcher_app, name="watcher")
app.add_typer(vendors_app, name="vendors")
app.add_typer(group_app, name="group")
app.add_typer(access_app, name="access")
app.add_typer(schedule_app, name="schedule")
app.add_typer(guest_app, name="guest")
app.add_typer(eclipse_app, name="eclipse")
app.add_typer(filter_app, name="filter")
app.add_typer(dns_app, name="dns")
# emoji=False: Rich would otherwise turn ":AB:" inside a MAC address into a glyph.
console = Console(emoji=False)

JSON_OPTION = typer.Option(False, "--json", help="Print machine readable JSON instead of a table.")
DEVICES_ARG = typer.Argument(help="One or more devices: MAC, nickname, router name or IP.")
EXTRA_MACS_ARG = typer.Argument(
    None, help="Extra MACs to unblock too, e.g. a device's old randomized address. Optional."
)
LAUNCHD_LABEL = "com.curfew.watch"


@dataclass
class Ctx:
    settings: Settings
    client: SoapClient
    registry: Registry
    vendors: VendorDb

    @property
    def devices(self) -> DeviceService:
        return DeviceService(self.client, self.registry, self.vendors)

    @property
    def control(self) -> ControlService:
        return ControlService(self.client, self.registry, self.settings.data_dir)

    @property
    def eclipse(self) -> EclipseService:
        return EclipseService(self.client, self.registry, self.settings)

    @property
    def filter(self) -> FilterService:
        return FilterService(self.registry)


def _run[T](fn: Callable[[Ctx], Awaitable[T]]) -> T:
    async def go() -> T:
        settings = Settings.from_env()
        registry = Registry(settings.registry_path)
        try:
            async with SoapClient(settings) as client:
                return await fn(Ctx(settings, client, registry, VendorDb(settings.data_dir)))
        finally:
            registry.close()

    try:
        return asyncio.run(go())
    except MissingCredentials as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(2) from err
    except SoapError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1) from err
    except httpx.HTTPError as err:
        console.print(f"[red]cannot reach the router: {type(err).__name__} {err}[/red]")
        raise typer.Exit(1) from err
    except (LookupError, ValueError, PauseError) as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1) from err


def _dump(value: Any) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
    json.dump(value, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _print_enforcement(enforced: Enforcement, *, prefix: str = "") -> None:
    for k, allow in enforced.devices:
        console.print(f"{prefix}{'allow' if allow else 'block':6} {k.display_name}  {k.mac}  (schedule)")
    for band, on in enforced.guest:
        console.print(f"{prefix}guest wifi {band.value} {'on' if on else 'off'}  (schedule)")


def _local(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


# -- router ------------------------------------------------------------------


@app.command()
def status(as_json: bool = JSON_OPTION) -> None:
    """Router model, firmware, uptime, WAN and LAN state."""
    s = _run(lambda c: status_service.get_status(c.client))
    if as_json:
        _dump(s)
        return
    t = Table(show_header=False, box=None)
    t.add_row("Model", f"{s.info.model}  ({s.info.description})")
    t.add_row("Firmware", s.info.firmware)
    t.add_row("Serial", s.info.serial_number)
    t.add_row("Uptime", s.uptime)
    t.add_row("CPU / memory", f"{s.system.cpu_utilization_percent}% / {s.system.memory_utilization_percent}%")
    t.add_row(
        "Internet",
        f"{'up' if s.wan.enabled else 'down'}, link {s.wan.ethernet_link or 'n/a'}, {s.wan.connection_type}",
    )
    t.add_row("External IP", f"{s.wan.external_ip}  gateway {s.wan.gateway}")
    t.add_row("DNS", " ".join(s.wan.dns_servers))
    t.add_row("LAN", f"{s.lan.ip} / {s.lan.subnet_mask}  DHCP {'on' if s.lan.dhcp_enabled else 'off'}")
    t.add_row("Access Control", "on" if s.access_control_enabled else "off")
    if s.device_count is not None:
        t.add_row("Devices", str(s.device_count))
    console.print(t)


@app.command()
def wifi(as_json: bool = JSON_OPTION) -> None:
    """SSIDs, channels, security and guest network state for both bands."""
    nets = _run(lambda c: status_service.get_wifi(c.client))
    if as_json:
        _dump(nets)
        return
    t = Table()
    for col in ("Band", "SSID", "Status", "Channel", "Mode", "Security", "Guest"):
        t.add_column(col)
    for n in nets:
        guest = "off" if not n.guest_enabled else f"on ({n.guest_ssid})"
        t.add_row(n.band.value, n.ssid, n.status, n.channel, n.wireless_mode, n.security, guest)
    console.print(t)


@app.command()
def traffic(as_json: bool = JSON_OPTION) -> None:
    """Traffic meter totals (only meaningful when the meter is enabled on the router)."""
    s = _run(lambda c: actions.get_traffic_stats(c.client))
    if as_json:
        _dump(s)
        return
    if not s.enabled:
        console.print(
            "[yellow]Traffic meter is disabled on the router; totals below will stay at zero.[/yellow]"
        )
    t = Table()
    for col in ("Period", "Download MB", "Upload MB", "Connected"):
        t.add_column(col)
    for label, p in (
        ("today", s.today),
        ("yesterday", s.yesterday),
        ("week", s.week),
        ("month", s.month),
        ("last month", s.last_month),
    ):
        connected = str(p.connection_time) if p.connection_time is not None else ""
        t.add_row(label, _fmt(p.download_mb), _fmt(p.upload_mb), connected)
    console.print(t)


@app.command()
def log(
    as_json: bool = JSON_OPTION,
    limit: int = typer.Option(50, help="Show at most this many entries, newest first."),
    category: str = typer.Option("", help="Only entries whose category contains this text, e.g. DHCP."),
) -> None:
    """Recent router log: DHCP leases, Access Control decisions, attack warnings."""
    entries = _run(lambda c: actions.get_system_logs(c.client))
    if category:
        entries = [e for e in entries if category.lower() in e.category.lower()]
    entries = entries[:limit]
    if as_json:
        _dump(entries)
        return
    for e in entries:
        ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S") if e.timestamp else " " * 19
        console.print(f"[dim]{ts}[/dim] [cyan]{e.category:16}[/cyan] {e.message}", soft_wrap=True)


@app.command()
def firmware(as_json: bool = JSON_OPTION) -> None:
    """Ask the router whether a firmware update is available (takes about 10 seconds)."""
    f = _run(lambda c: actions.check_firmware(c.client))
    if as_json:
        _dump(f)
        return
    if f.update_available:
        console.print(f"[yellow]Update available: {f.current_version} -> {f.new_version}[/yellow]")
        if f.release_note:
            console.print(f.release_note)
    else:
        console.print(f"Firmware {f.current_version} is current.")


# -- devices -----------------------------------------------------------------


@app.command()
def devices(
    as_json: bool = JSON_OPTION,
    sort: str = typer.Option("ip", help="Sort by: ip, name, band, signal, owner, seen."),
) -> None:
    """List every device currently attached, with nicknames and owners from the registry."""
    devs = _run(lambda c: c.devices.list_online())
    devs.sort(key=_sort_key(sort))
    if as_json:
        _dump(devs)
        return
    now = datetime.now(UTC)
    any_blocked = any(not d.allowed for d in devs)
    t = Table(title=f"{len(devs)} attached devices", box=box.SIMPLE_HEAD, pad_edge=False)
    t.add_column("IP", no_wrap=True)
    t.add_column("Name", no_wrap=True, max_width=24)
    t.add_column("Owner", no_wrap=True, max_width=12)
    t.add_column("MAC", no_wrap=True)
    t.add_column("Band", no_wrap=True)
    t.add_column("Sig", justify="right", no_wrap=True)
    t.add_column("Type", no_wrap=True, max_width=20)
    t.add_column("Vendor / model", no_wrap=True, max_width=26)
    t.add_column("First seen", no_wrap=True)
    if any_blocked:
        t.add_column("Access", no_wrap=True)
    for d in devs:
        name = d.display_name
        if not d.nickname and not d.is_known:
            name = f"[dim]{name}[/dim]"
        mac = f"{d.mac}[dim]*[/dim]" if d.randomized_mac else d.mac
        row = [
            d.ip,
            name,
            d.owner,
            mac,
            d.band.value,
            f"{d.signal_percent}%" if d.signal_percent is not None else "",
            d.device_type.replace(" (Generic)", ""),
            d.model or d.vendor,
            humanize(d.first_seen, now=now),
        ]
        if any_blocked:
            row.append("allow" if d.allowed else "[red]BLOCK[/red]")
        t.add_row(*row)
    console.print(t)
    if any(d.randomized_mac for d in devs):
        console.print(
            "[dim]* randomized MAC (private wifi address); it may change, so give the device a nickname[/dim]"
        )


def _sort_key(field_name: str) -> Callable[[DeviceView], Any]:
    if field_name == "ip":
        return lambda d: tuple(int(p) for p in d.ip.split(".")) if d.ip.count(".") == 3 else (999,)
    if field_name == "name":
        return lambda d: (not (d.nickname or d.is_known), d.display_name.lower())
    if field_name == "band":
        return lambda d: (d.band.value, d.display_name.lower())
    if field_name == "signal":
        return lambda d: -(d.signal_percent or 0)
    if field_name == "owner":
        return lambda d: (d.owner == "", d.owner.lower(), d.display_name.lower())
    if field_name == "seen":
        return lambda d: d.first_seen or datetime.min.replace(tzinfo=UTC)
    raise typer.BadParameter(f"unknown sort field {field_name!r}")


@app.command()
def scan(as_json: bool = JSON_OPTION) -> None:
    """One registry refresh: prints devices that are new, came back, or left."""
    delta = _run(lambda c: c.devices.scan())
    if as_json:
        _dump(delta)
        return
    console.print(f"{delta.present} devices online at {_local(delta.scanned_at)}")
    for label, items, colour in (
        ("new", delta.new, "yellow"),
        ("returned", delta.returned, "green"),
        ("left", delta.left, "dim"),
    ):
        for k in items:
            console.print(
                f"  [{colour}]{label:8}[/{colour}] {k.display_name}  {k.mac}  {k.last_ip}  {k.vendor}"
            )


@app.command()
def new(
    since: str = typer.Option("7d", help="Window: 7d, 24h, 30m, or an ISO date."),
    as_json: bool = JSON_OPTION,
) -> None:
    """Devices first seen within the window. Runs a scan first."""

    async def go(c: Ctx) -> list[KnownDevice]:
        await c.devices.scan()
        return c.devices.new_since(parse_since(since))

    items = _run(go)
    if as_json:
        _dump(items)
        return
    if not items:
        console.print(f"No new devices since {since}.")
        return
    _print_known(items)


@app.command()
def known(
    owner: str = typer.Option("", help="Only devices with this owner."),
    tag: str = typer.Option("", help="Only devices with this tag."),
    online: bool = typer.Option(False, "--online", help="Only devices online right now."),
    as_json: bool = JSON_OPTION,
) -> None:
    """Everything the registry remembers, online or not."""
    items = _run(lambda c: _async_value(c.registry.all()))
    if owner:
        items = [k for k in items if k.owner.lower() == owner.lower()]
    if tag:
        items = [k for k in items if tag.lower() in (t.lower() for t in k.tags)]
    if online:
        items = [k for k in items if k.online]
    if as_json:
        _dump(items)
        return
    _print_known(items)


def _print_known(items: list[KnownDevice]) -> None:
    now = datetime.now(UTC)
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    widths = {"Name": 26, "Owner": 12, "Tags": 14, "Vendor / model": 28}
    for col in (
        "Name",
        "Owner",
        "Tags",
        "MAC",
        "Last IP",
        "Vendor / model",
        "First seen",
        "Last seen",
        "Online",
    ):
        t.add_column(col, no_wrap=True, max_width=widths.get(col))
    for k in items:
        t.add_row(
            k.display_name,
            k.owner,
            ",".join(k.tags),
            f"{k.mac}[dim]*[/dim]" if k.randomized_mac else k.mac,
            k.last_ip,
            k.last_model or k.vendor,
            humanize(k.first_seen, now=now),
            humanize(k.last_seen, now=now),
            "[green]yes[/green]" if k.online else "no",
        )
    console.print(t)


@app.command()
def name(
    device: str = typer.Argument(help="MAC, current nickname, router name or IP."),
    nickname: str | None = typer.Option(None, help="Friendly name. Empty string clears."),
    owner: str | None = typer.Option(None, help="Who it belongs to. Empty string clears."),
    tags: str | None = typer.Option(None, help="Comma separated tags, e.g. kid,phone. Empty string clears."),
    notes: str | None = typer.Option(None, help="Free text."),
) -> None:
    """Give a device a nickname, owner, tags or notes in the local registry."""
    tag_list = [t for t in tags.split(",")] if tags is not None else None
    k = _run(
        lambda c: _async_value(
            c.devices.name(device, nickname=nickname, owner=owner, tags=tag_list, notes=notes)
        )
    )
    console.print(f"{k.mac}: name={k.display_name!r} owner={k.owner!r} tags={k.tags} notes={k.notes!r}")


@app.command()
def merge(
    old: str = typer.Argument(help="The duplicate to remove: MAC, nickname, router name or IP."),
    new: str = typer.Argument(help="The record to keep: MAC, nickname, router name or IP."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Merge one device record into another, then forget the old one.

    Use it when the same device shows up twice, e.g. after it stops using a randomized
    wifi MAC and rejoins with its real one. The new record keeps its own fields and
    inherits the old nickname, owner, tags and history.
    """
    old_dev, new_dev = _run(lambda c: _async_value((c.devices.resolve(old), c.devices.resolve(new))))
    if old_dev.mac == new_dev.mac:
        console.print(f"[red]{old!r} and {new!r} are the same device; nothing to merge.[/red]")
        raise typer.Exit(1)
    console.print(
        f"Merge [bold]{old_dev.display_name}[/bold] ({old_dev.mac}) "
        f"into [bold]{new_dev.display_name}[/bold] ({new_dev.mac}) and forget the old record."
    )
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Exit(0)
    _, merged = _run(lambda c: _async_value(c.devices.merge(old, new)))
    console.print(
        f"Merged into {merged.mac}: name={merged.display_name!r} owner={merged.owner!r} "
        f"tags={merged.tags}. Removed {old_dev.mac}."
    )


@app.command()
def history(
    device: str = typer.Argument(help="MAC, nickname, router name or IP."),
    limit: int = typer.Option(20, help="Number of sessions to show."),
    as_json: bool = JSON_OPTION,
) -> None:
    """When a device joined and left the network."""
    k, sessions = _run(lambda c: _async_value(c.devices.history(device, limit=limit)))
    if as_json:
        _dump(
            {"device": k.model_dump(mode="json"), "sessions": [s.model_dump(mode="json") for s in sessions]}
        )
        return
    console.print(f"{k.display_name}  {k.mac}  owner={k.owner or '-'}  first seen {_local(k.first_seen)}")
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for col in ("Joined", "Left", "Duration", "IP", "Band"):
        t.add_column(col, no_wrap=True)
    for s in sessions:
        minutes = int(s.duration.total_seconds() // 60)
        t.add_row(
            _local(s.started_at),
            _local(s.ended_at) if s.ended_at else "[green]online[/green]",
            f"{minutes // 60}h {minutes % 60:02d}m",
            s.ip,
            s.band,
        )
    console.print(t)


@app.command()
def watch(
    interval: int = typer.Option(60, help="Seconds between scans."),
    once: bool = typer.Option(False, "--once", help="Scan one time and exit (same as `scan`)."),
) -> None:
    """Scan on an interval and enforce every schedule after each scan. Prints changes as they happen."""

    async def go(c: Ctx) -> None:
        while True:
            started = datetime.now(UTC)
            try:
                delta, enforced = await c.control.tick()
            except SoapError as err:
                console.print(f"[red]{_local(started)} scan failed: {err}[/red]")
            else:
                changes = [("new", k) for k in delta.new] + [("returned", k) for k in delta.returned]
                changes += [("left", k) for k in delta.left]
                for label, k in changes:
                    console.print(
                        f"{_local(started)} {label:8} {k.display_name}  {k.mac}  {k.last_ip}  {k.vendor}"
                    )
                _print_enforcement(enforced, prefix=f"{_local(started)} ")
                if not changes and not enforced.devices and not enforced.guest:
                    console.print(f"[dim]{_local(started)} {delta.present} online, no changes[/dim]")
            if once:
                return
            elapsed = (datetime.now(UTC) - started).total_seconds()
            await asyncio.sleep(max(5.0, interval - elapsed))

    _run(go)


# -- groups --------------------------------------------------------------------


@group_app.command("list")
def group_list() -> None:
    """Groups and their members."""
    groups = _run(lambda c: _async_value(c.registry.groups()))
    if not groups:
        console.print("No groups yet. Create one with: curfew group add kids <device>")
        return
    for name, members in groups.items():
        console.print(f"[bold]{name}[/bold] ({len(members)})")
        for m in members:
            state = "[red]blocked[/red]" if m.access_state == "block" else "allowed"
            console.print(f"  {m.display_name:28} {m.mac}  {m.owner or '-':10} {state}")


@group_app.command("add")
def group_add(
    group: str = typer.Argument(help="Group name, e.g. kids."),
    device: list[str] = DEVICES_ARG,
) -> None:
    """Add devices to a group."""

    async def go(c: Ctx) -> list[KnownDevice]:
        return [c.registry.add_tags(c.devices.resolve(d).mac, [group]) for d in device]

    for k in _run(go):
        console.print(f"{k.display_name} ({k.mac}) -> groups {k.tags}")


@group_app.command("remove")
def group_remove(
    group: str = typer.Argument(help="Group name."),
    device: list[str] = DEVICES_ARG,
) -> None:
    """Remove devices from a group."""

    async def go(c: Ctx) -> list[KnownDevice]:
        return [c.registry.remove_tags(c.devices.resolve(d).mac, [group]) for d in device]

    for k in _run(go):
        console.print(f"{k.display_name} ({k.mac}) -> groups {k.tags}")


# -- access --------------------------------------------------------------------


def _access_change(target: str, allow: bool, reason: str) -> None:
    change = _run(lambda c: c.control.set_access(target, allow=allow, reason=reason))
    verb = "allowed" if allow else "blocked"
    console.print(f"{verb} {change.kind} [bold]{change.target}[/bold]: {len(change.devices)} device(s)")
    for d in change.devices:
        console.print(f"  {d.display_name:28} {d.mac}  {d.last_ip}")
    if change.until:
        console.print(f"[dim]until the next scheduled change at {_local(change.until)}[/dim]")
    if change.access_control_enabled_now:
        console.print("[yellow]Access Control was off on the router and has been enabled.[/yellow]")


@access_app.command("off")
def access_off(
    target: str = typer.Argument(help="Group, owner, or device."),
    reason: str = typer.Option("", help="Free text for the change log."),
) -> None:
    """Block internet access for a group, owner or device."""
    _access_change(target, False, reason)


@access_app.command("on")
def access_on(
    target: str = typer.Argument(help="Group, owner, or device."),
    reason: str = typer.Option("", help="Free text for the change log."),
) -> None:
    """Restore internet access for a group, owner or device."""
    _access_change(target, True, reason)


@access_app.command("status")
def access_status() -> None:
    """Access Control state, blocked devices, active overrides and schedules."""

    async def go(c: Ctx) -> tuple[bool, list[KnownDevice], dict[str, AccessOverride], list[Rule]]:
        enabled = await actions.get_access_control_enabled(c.client)
        await c.devices.scan()
        return enabled, c.registry.all(), c.registry.overrides(), c.registry.rules()

    enabled, known, overrides, rules = _run(go)
    console.print(f"Access Control on router: {'[green]on[/green]' if enabled else 'off'}")
    now_local = datetime.now().astimezone()
    blocked = [k for k in known if k.access_state == "block"]
    console.print(f"Blocked devices: {len(blocked)}")
    for k in blocked:
        o = overrides.get(k.mac)
        why = f"manual{' until ' + _local(o.until) if o and o.until else ''}" if o else "schedule"
        console.print(f"  {k.display_name:28} {k.mac}  {why}")
    active = [r for r in rules if is_active(r, now_local)]
    if active:
        console.print("Active schedules: " + ", ".join(r.name for r in active))


@access_app.command("control")
def access_control(state: str = typer.Argument(help="on or off")) -> None:
    """Turn the router's Access Control feature on or off (off means no device is blocked)."""
    if state not in {"on", "off"}:
        raise typer.BadParameter("use on or off")
    _run(lambda c: c.control.set_access_control(state == "on"))
    console.print(f"Access Control {state}")


@access_app.command("clear")
def access_clear(
    macs: list[str] | None = EXTRA_MACS_ARG,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Unblock every device curfew has blocked, clearing the Access Control deny list.

    The router exposes no way to read its full block list, so this clears the blocks curfew knows
    about: devices flagged blocked and any pending block overrides. Name extra MACs to also clear
    orphaned entries the registry no longer tracks, such as an old randomized MAC after a merge.
    """
    extra = macs or []

    async def plan(c: Ctx) -> list[tuple[str, str]]:
        await c.devices.scan()
        out: list[tuple[str, str]] = []
        for mac in c.control.blocked_macs(extra):
            known = c.registry.get(mac)
            out.append((mac, known.display_name if known else ""))
        return out

    planned = _run(plan)
    if not planned:
        console.print("Nothing to clear: no blocked devices known to curfew.")
        raise typer.Exit(0)
    console.print(f"Will unblock {len(planned)} device(s) in Access Control:")
    for mac, name in planned:
        console.print(f"  {name or '(unknown)':28} {mac}")
    if not yes and not typer.confirm("Clear these from the deny list?"):
        raise typer.Exit(0)
    cleared = _run(lambda c: c.control.clear_access_control(extra))
    console.print(f"Cleared {len(cleared)} device(s) from the Access Control deny list.")


# -- schedules -----------------------------------------------------------------


@schedule_app.command("add")
def schedule_add(
    target: str = typer.Argument(help="Group, owner, or device to block during the window."),
    start: str = typer.Option(..., "--from", help="Start time, e.g. 21:00."),
    end: str = typer.Option(..., "--to", help="End time, e.g. 07:00 (may be next morning)."),
    days: str = typer.Option("daily", help="daily, weekdays, weekends, mon-fri, sat,sun ..."),
    name: str = typer.Option("", help="Label, e.g. bedtime."),
) -> None:
    """Block a target every day between two times."""
    rule = _run(
        lambda c: _async_value(c.control.add_schedule(target, start=start, end=end, days=days, name=name))
    )
    console.print(
        f"#{rule.id} {rule.name}: block {rule.kind} {rule.target} "
        f"{rule.start}-{rule.end} {format_days(rule.days)}"
    )


@schedule_app.command("list")
def schedule_list() -> None:
    """Show schedules and whether each is active right now."""
    rules = _run(lambda c: _async_value(c.registry.rules()))
    if not rules:
        console.print("No schedules. Add one with: curfew schedule add kids --from 21:00 --to 07:00")
        return
    now_local = datetime.now().astimezone()
    for r in rules:
        state = "[red]active[/red]" if is_active(r, now_local) else ("off" if not r.enabled else "idle")
        what = f"guest wifi off {r.target:9}" if r.kind == "guest" else f"block {r.kind} {r.target:16}"
        console.print(f"#{r.id:<3} {r.name:24} {what} {r.start}-{r.end} {format_days(r.days):10} {state}")


@schedule_app.command("remove")
def schedule_remove(rule_id: int = typer.Argument(help="Schedule id from `schedule list`.")) -> None:
    """Delete a schedule. Devices it blocked are released on the next apply."""
    removed = _run(lambda c: _async_value(c.control.remove_schedule(rule_id)))
    console.print("removed" if removed else "no such schedule")


@schedule_app.command("apply")
def schedule_apply() -> None:
    """Evaluate schedules and overrides now and push any needed changes to the router."""

    async def go(c: Ctx) -> Enforcement:
        _, enforced = await c.control.tick()
        return enforced

    enforced = _run(go)
    if not enforced.devices and not enforced.guest:
        console.print("Nothing to change.")
    _print_enforcement(enforced)


# -- reboot and guest wifi -----------------------------------------------------------


@app.command()
def reboot(yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt.")) -> None:
    """Reboot the router. Everyone loses connectivity for a couple of minutes."""
    if not yes and not typer.confirm("Reboot the router now?"):
        raise typer.Exit(0)
    _run(lambda c: c.control.reboot())
    console.print("Reboot requested. The router will be back in about two minutes.")


@guest_app.command("on")
def guest_on(band: str = typer.Option("both", help="2.4, 5, or both.")) -> None:
    """Enable the guest wifi network."""
    _guest(band, True)


@guest_app.command("off")
def guest_off(band: str = typer.Option("both", help="2.4, 5, or both.")) -> None:
    """Disable the guest wifi network."""
    _guest(band, False)


@guest_app.command("schedule")
def guest_schedule(
    start: str = typer.Option(..., "--from", help="When the guest network turns off, e.g. 21:00."),
    end: str = typer.Option(..., "--to", help="When it turns back on, e.g. 07:00 (may be next morning)."),
    days: str = typer.Option("daily", help="daily, weekdays, weekends, mon-fri, sat,sun ..."),
    band: str = typer.Option("both", help="2.4, 5, or both."),
    name: str = typer.Option("", help="Label, e.g. guest-night."),
) -> None:
    """Turn the guest network off every day between two times, and back on after.

    Enforced by `curfew watch` (or the watcher service) at each scan. A manual `guest on/off`
    in between is respected until the next window boundary. Remove with `curfew schedule remove <id>`.
    """
    rule = _run(
        lambda c: _async_value(
            c.control.add_guest_schedule(start=start, end=end, days=days, band=band, name=name)
        )
    )
    console.print(
        f"#{rule.id} {rule.name}: guest wifi {rule.target} off {rule.start}-{rule.end} "
        f"{format_days(rule.days)}"
    )


def _guest(band: str, enabled: bool) -> None:
    bands = {"2.4": [Band.GHZ_2_4], "5": [Band.GHZ_5], "both": [Band.GHZ_2_4, Band.GHZ_5]}.get(band)
    if bands is None:
        raise typer.BadParameter("band must be 2.4, 5 or both")

    async def go(c: Ctx) -> None:
        for b in bands:
            await c.control.set_guest_wifi(b, enabled)

    _run(go)
    console.print(f"Guest wifi {'on' if enabled else 'off'} for {', '.join(b.value for b in bands)}")


# -- eclipse pause -------------------------------------------------------------


@app.command()
def pause(
    device: str = typer.Argument(help="Group, owner, or device to cut off the internet."),
    reason: str = typer.Option("", help="Free text for the change log."),
) -> None:
    """Instantly cut a device (or group/owner) off the internet via Eclipse Pause (ARP).

    Records the pause; the `eclipse` daemon enforces it within a couple of seconds.
    Keeps the device on wifi and on the LAN, like Circle's pause. Needs the daemon running as root.
    """

    async def go(c: Ctx) -> list[Any]:
        kind, members = c.control.resolve_target(device)
        await c.devices.scan()
        out = []
        for m in members:
            fresh = c.registry.get(m.mac) or m
            if not fresh.last_ip:
                console.print(f"[yellow]skipping {fresh.display_name}: no current IP (offline?)[/yellow]")
                continue
            c.registry.set_pause(m.mac, fresh.last_ip, reason)
            out.append(fresh)
        return out

    paused = _run(go)
    for k in paused:
        console.print(f"paused {k.display_name}  {k.mac}  {k.last_ip}")
    if not eclipse_running(Settings.from_env().data_dir):
        console.print(
            "[yellow]Eclipse daemon is not running; pause is recorded but not enforced yet.[/yellow]"
        )
        console.print("Start it with: [bold]sudo -E uv run curfew eclipse run[/bold]")


@app.command()
def resume(
    device: str = typer.Argument("", help="Group, owner, or device to restore. Omit with --all."),
    all_: bool = typer.Option(False, "--all", help="Resume every paused device."),
) -> None:
    """Lift an Eclipse Pause. The daemon heals the device's ARP within a couple of seconds."""

    async def go(c: Ctx) -> int:
        if all_:
            return c.eclipse.resume_all()
        if not device:
            raise typer.BadParameter("name a device or pass --all")
        _, members = c.control.resolve_target(device)
        return sum(1 for m in members if c.registry.clear_pause(m.mac))

    n = _run(go)
    console.print(f"resumed {n} device(s)")


@app.command()
def alloff(
    group: str = typer.Option("admin", "--except", help="Group to keep online. Default: admin."),
    reason: str = typer.Option("", help="Free text for the change log."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Instantly cut the internet for every attached device except the admin group (Eclipse Pause).

    Put your own computer, home server and anything that must stay online in the `admin` group first:
    curfew group add admin "My Mac" "Home Server". Needs the eclipse daemon running as root.
    """

    async def go(c: Ctx) -> list[Any]:
        await c.devices.scan()
        plan = c.eclipse.devices_to_pause(group)
        exempt = c.registry.by_tag(group)
        if not plan:
            console.print(f"Nothing to cut: no eligible online devices outside '{group}'.")
            return []
        console.print(
            f"Will cut internet for {len(plan)} device(s), keeping '{group}' ({len(exempt)}) online:"
        )
        for d in plan:
            console.print(f"  {d.display_name:28} {d.mac}  {d.last_ip}")
        if not exempt:
            console.print(f"[yellow]Warning: group '{group}' is empty, so this host may cut itself.[/yellow]")
        if not yes and not typer.confirm("Proceed?"):
            return []
        for d in plan:
            c.registry.set_pause(d.mac, d.last_ip, reason)
        return plan

    paused = _run(go)
    if paused:
        console.print(f"Cut {len(paused)} device(s) off the internet. '{group}' stays online.")
    if paused and not eclipse_running(Settings.from_env().data_dir):
        console.print(
            "[yellow]Eclipse daemon is not running; pauses are recorded but not enforced yet.[/yellow]"
        )
        console.print("Start it with: [bold]sudo -E uv run curfew eclipse run[/bold]")


@app.command()
def allon() -> None:
    """Restore the internet for every paused device, lifting all Eclipse Pauses at once."""
    n = _run(lambda c: _async_value(c.eclipse.resume_all()))
    console.print(f"resumed {n} device(s)")


@eclipse_app.command("run")
def eclipse_run(
    interval: float = typer.Option(1.0, help="Seconds between ARP re-sends. Lower holds stubborn devices."),
    iface: str = typer.Option("", help="Network interface (default: auto)."),
    one_way: bool = typer.Option(
        False, "--one-way", help="Only poison the victim, not the router. Weaker; two-way is the default."
    ),
) -> None:
    """Run the enforcement daemon in the foreground. Needs root (sudo -E).

    By default it poisons both directions: it tells the victim the router is at our MAC and tells the
    router the victim is at our MAC, so a device cannot recover a working path between re-sends. This
    reliably cuts a streaming device without a reboot; `--one-way` reverts to victim-only poisoning.
    """
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    settings = Settings.from_env()
    try:
        asyncio.run(run_daemon(settings, interval=interval, iface=iface or None, bidirectional=not one_way))
    except PauseError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1) from err


@eclipse_app.command("status")
def eclipse_status() -> None:
    """Show whether the daemon is enforcing and which devices are paused."""
    settings = Settings.from_env()
    running = eclipse_running(settings.data_dir)
    registry = Registry(settings.registry_path)
    try:
        paused = registry.pauses()
    finally:
        registry.close()
    console.print(f"Eclipse daemon: {'[green]enforcing[/green]' if running else '[red]not running[/red]'}")
    if not paused:
        console.print("No devices paused.")
        return
    for p in paused:
        console.print(f"  {p.display_name or '?':24} {p.mac}  {p.ip}  since {_local(p.since)}  {p.reason}")


@eclipse_app.command("verify")
def eclipse_verify(
    device: str = typer.Argument(help="Device to test: MAC, nickname, router name or IP."),
    seconds: float = typer.Option(8.0, help="How long to spoof and sniff."),
    iface: str = typer.Option("", help="Interface (default: auto)."),
) -> None:
    """Prove Eclipse works: briefly redirect a device and confirm its traffic arrives at us. Needs root.

    Safe and self-healing: it spoofs for a few seconds, watches for the device's packets coming to
    this host, then restores the device's ARP. If packets are captured, interception works.
    """
    from scapy.all import AsyncSniffer, Ether  # type: ignore[attr-defined]

    settings = Settings.from_env()

    async def resolve(c: Ctx) -> tuple[str, str, str]:
        known = c.devices.resolve(device)
        await c.devices.scan()
        fresh = c.registry.get(known.mac) or known
        return known.mac, fresh.last_ip, fresh.display_name

    mac, ip, name = _run(resolve)
    if not ip:
        console.print(f"[red]{name} has no current IP (offline?)[/red]")
        raise typer.Exit(1)
    try:
        net = build_network_info(settings.host, iface or None)
    except PauseError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1) from err

    from curfew.pause import PauseEngine, ScapySender, Target

    target = Target(mac=mac, ip=ip)
    engine = PauseEngine(net, ScapySender(net.iface))
    captured = {"n": 0}

    def seen(pkt: object) -> None:
        eth = pkt.getlayer(Ether) if hasattr(pkt, "getlayer") else None
        if eth is not None and eth.src.upper() == mac.upper() and eth.dst.upper() == net.our_mac.upper():
            captured["n"] += 1

    console.print(f"Testing {name} ({mac} / {ip}) for {seconds:.0f}s ...")
    sniffer = AsyncSniffer(iface=net.iface, prn=seen, store=False)
    sniffer.start()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            engine.spoof(target)
            time.sleep(1.0)
    finally:
        engine.heal(target)
        engine.close()
        sniffer.stop()
    if captured["n"] > 0:
        console.print(f"[green]OK[/green] captured {captured['n']} redirected packet(s): interception works.")
    else:
        console.print(
            "[yellow]No packets captured.[/yellow] The device may be idle; try while it is streaming, "
            "or confirm from the device that the internet dropped during the test."
        )


@eclipse_app.command("doctor")
def eclipse_doctor(iface: str = typer.Option("", help="Interface to test (default: auto).")) -> None:
    """Resolve and print the network context Eclipse needs. Run with sudo to test raw sockets."""
    settings = Settings.from_env()
    try:
        net = build_network_info(settings.host, iface or None)
    except PauseError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1) from err
    console.print(f"Interface : {net.iface}")
    console.print(f"This host : {net.our_ip}  {net.our_mac}")
    console.print(f"Router    : {net.gateway_ip}  {net.gateway_mac}")
    console.print("[green]OK[/green] Eclipse can resolve the router; the daemon can enforce pauses.")


# -- DNS website filter -------------------------------------------------------

FILTER_TARGET_ARG = typer.Argument(help="Group, owner, device, or 'all' for everyone.")
FILTER_DOMAINS_ARG = typer.Argument(help="One or more domains, e.g. youtube.com tiktok.com.")


def _looks_like_ip(pattern: str) -> bool:
    head = pattern.split("/")[0]
    parts = head.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


@filter_app.command("mode")
def filter_mode(
    target: str = FILTER_TARGET_ARG,
    mode: str = typer.Argument(help="off, blacklist (block the list), or whitelist (allow only the list)."),
) -> None:
    """Set the filtering mode for a group, owner, device, or everyone."""
    label, applied = _run(lambda c: _async_value(c.filter.set_mode(target, mode)))
    console.print(f"{label}: filter mode = {applied}")
    if not dns_running(Settings.from_env().data_dir):
        console.print(
            "[yellow]The DNS filter daemon is not running; start it with: "
            "sudo -E uv run curfew dns run[/yellow]"
        )


@filter_app.command("block")
def filter_block(target: str = FILTER_TARGET_ARG, domains: list[str] = FILTER_DOMAINS_ARG) -> None:
    """Add domains to a target's block list (used in blacklist mode)."""

    def go(c: Ctx) -> list[str]:
        added = []
        for d in domains:
            if _looks_like_ip(d):
                console.print(
                    f"[yellow]{d} looks like an IP; the DNS filter matches domains, "
                    "so it is stored but not enforced yet.[/yellow]"
                )
            _, p = c.filter.add(target, d, block=True)
            added.append(p)
        return added

    added = _run(lambda c: _async_value(go(c)))
    console.print(f"blocked {len(added)} pattern(s): {', '.join(added)}")


@filter_app.command("allow")
def filter_allow(target: str = FILTER_TARGET_ARG, domains: list[str] = FILTER_DOMAINS_ARG) -> None:
    """Add domains to a target's allow list (the only ones reachable in whitelist mode)."""
    added = _run(lambda c: _async_value([c.filter.add(target, d, block=False)[1] for d in domains]))
    console.print(f"allowed {len(added)} pattern(s): {', '.join(added)}")


@filter_app.command("unblock")
def filter_unblock(target: str = FILTER_TARGET_ARG, domains: list[str] = FILTER_DOMAINS_ARG) -> None:
    """Remove domains from a target's block list."""
    n = _run(lambda c: _async_value(sum(c.filter.remove(target, d, block=True) for d in domains)))
    console.print(f"removed {n} block pattern(s)")


@filter_app.command("unallow")
def filter_unallow(target: str = FILTER_TARGET_ARG, domains: list[str] = FILTER_DOMAINS_ARG) -> None:
    """Remove domains from a target's allow list."""
    n = _run(lambda c: _async_value(sum(c.filter.remove(target, d, block=False) for d in domains)))
    console.print(f"removed {n} allow pattern(s)")


@filter_app.command("list")
def filter_list(
    target: str = typer.Argument("", help="Narrow to one group, owner or device. Omit for all scopes."),
) -> None:
    """Show filtering modes and the allow/deny lists."""

    def go(c: Ctx) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str, str]]]:
        rules = c.filter.rules(target or None)
        return c.filter.modes(), rules

    modes, rules = _run(lambda c: _async_value(go(c)))
    if modes:
        console.print("[bold]Modes[/bold]")
        for kind, value, mode in modes:
            console.print(f"  {kind}:{value or 'all':20} {mode}")
    else:
        console.print("No filtering modes set (everything is off).")
    if rules:
        console.print("[bold]Rules[/bold]")
        for kind, value, list_kind, pattern in rules:
            console.print(f"  [{list_kind:5}] {kind}:{value or 'all':20} {pattern}")


@filter_app.command("status")
def filter_status() -> None:
    """Whether the DNS filter daemon is running and a summary of scopes with a mode set."""
    running = dns_running(Settings.from_env().data_dir)
    console.print(f"DNS filter daemon: {'[green]running[/green]' if running else '[red]not running[/red]'}")
    modes = _run(lambda c: _async_value(c.filter.modes()))
    if not modes:
        console.print("No scopes are filtered.")
        return
    for kind, value, mode in modes:
        console.print(f"  {kind}:{value or 'all':20} {mode}")


@dns_app.command("run")
def dns_run(
    upstream: str = typer.Option(DEFAULT_UPSTREAM, help="Upstream resolver to forward allowed queries to."),
    port: int = typer.Option(53, help="UDP port to listen on."),
    listen: str = typer.Option("0.0.0.0", help="Address to bind."),
) -> None:
    """Run the DNS filter daemon in the foreground. Needs root for port 53 (sudo -E).

    Point the router's DHCP DNS at this machine so every device resolves through it.
    """
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    settings = Settings.from_env()
    try:
        asyncio.run(run_dns(settings, upstream=upstream, port=port, listen_host=listen))
    except PermissionError as err:
        console.print(f"[red]cannot bind port {port} (need root?): {err}[/red]")
        raise typer.Exit(1) from err


@dns_app.command("status")
def dns_status() -> None:
    """Show whether the DNS filter daemon is enforcing."""
    running = dns_running(Settings.from_env().data_dir)
    console.print(f"DNS filter daemon: {'[green]running[/green]' if running else '[red]not running[/red]'}")


# -- background watcher (launchd) -------------------------------------------


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@watcher_app.command("install")
def watcher_install(interval: int = typer.Option(60, help="Seconds between scans.")) -> None:
    """Write and load a launchd agent that runs `curfew watch` at login."""
    if sys.platform != "darwin":
        console.print("[red]launchd is macOS only[/red]")
        raise typer.Exit(1)
    uv = shutil.which("uv")
    if not uv:
        console.print("[red]uv not found on PATH[/red]")
        raise typer.Exit(1)
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            uv,
            "run",
            "--directory",
            str(_project_root()),
            "curfew",
            "watch",
            "--interval",
            str(interval),
        ],
        "WorkingDirectory": str(_project_root()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(settings.data_dir / "watch.log"),
        "StandardErrorPath": str(settings.data_dir / "watch.err.log"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_COLOR": "1"},
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.write_bytes(plistlib.dumps(plist))
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        console.print(f"[red]launchctl bootstrap failed: {result.stderr.strip()}[/red]")
        raise typer.Exit(1)
    console.print(f"Installed {path}. Log: {plist['StandardOutPath']}")


@watcher_app.command("uninstall")
def watcher_uninstall() -> None:
    """Stop and remove the launchd agent."""
    path = _plist_path()
    if not path.exists():
        console.print("Watcher is not installed.")
        return
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.unlink()
    console.print("Watcher removed.")


@watcher_app.command("status")
def watcher_status() -> None:
    """Show whether the launchd agent is loaded and running."""
    path = _plist_path()
    if not path.exists():
        console.print("Watcher is not installed.")
        return
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        console.print("Installed but not loaded.")
        return
    state = next((line.strip() for line in result.stdout.splitlines() if "state =" in line), "state unknown")
    pid = next((line.strip() for line in result.stdout.splitlines() if "pid =" in line), "")
    console.print(f"{state}  {pid}".strip())


# -- vendors ------------------------------------------------------------------


@vendors_app.command("update")
def vendors_update() -> None:
    """Download the IEEE OUI table so unknown devices show a manufacturer."""
    settings = Settings.from_env()
    try:
        count = asyncio.run(VendorDb(settings.data_dir).update())
    except httpx.HTTPError as err:
        console.print(f"[red]download failed: {err}[/red]")
        raise typer.Exit(1) from err
    console.print(f"Loaded {count} vendor prefixes into {settings.data_dir / 'oui.csv'}")


# -- mcp ----------------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio (what .mcp.json points at)."""
    from curfew.mcp_server import main as mcp_main

    mcp_main()


# -- helpers ------------------------------------------------------------------


async def _async_value[T](value: T) -> T:
    return value


def _fmt(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else ""


if __name__ == "__main__":
    app()
