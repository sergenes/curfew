"""Changes to the router: access on/off for devices, owners and groups, schedules, reboot, guest wifi.

Every change is appended to <data_dir>/changes.log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from curfew import actions
from curfew.models import AccessChange, Band, KnownDevice, Rule, ScanDelta
from curfew.registry import Registry
from curfew.scheduler import (
    GUEST_KIND,
    GUEST_TARGETS,
    desired_guest,
    desired_state,
    format_days,
    next_boundary,
    override_expired,
    parse_days,
    parse_hhmm,
    rule_applies_to,
)
from curfew.services.devices import DeviceService
from curfew.soap import SoapClient
from curfew.vendor import normalize_mac

log = logging.getLogger(__name__)


@dataclass
class Enforcement:
    """What one schedule pass changed: device access, and the guest network per band."""

    devices: list[tuple[KnownDevice, bool]] = field(default_factory=list)
    guest: list[tuple[Band, bool]] = field(default_factory=list)


class ControlService:
    def __init__(self, client: SoapClient, registry: Registry, data_dir: Path) -> None:
        self.client = client
        self.registry = registry
        self.devices = DeviceService(client, registry)
        self.changes_log = data_dir / "changes.log"

    # -- targets -----------------------------------------------------------

    def resolve_target(self, target: str) -> tuple[str, list[KnownDevice]]:
        """A target is a group (tag), an owner, or a single device. Groups win, then owners, then devices."""
        t = target.strip()
        groups = self.registry.groups()
        for name, members in groups.items():
            if name.lower() == t.lower():
                return "group", members
        owned = self.registry.by_owner(t)
        if owned:
            return "owner", owned
        return "device", [self.devices.resolve(t)]

    # -- access on / off -----------------------------------------------------

    async def set_access(
        self, target: str, *, allow: bool, reason: str = "", now: datetime | None = None
    ) -> AccessChange:
        kind, members = self.resolve_target(target)
        if not members:
            raise LookupError(f"{target!r} has no devices")

        enabled_now = False
        if not allow and not await actions.get_access_control_enabled(self.client):
            await actions.set_access_control_enabled(self.client, True)
            enabled_now = True
            self._log("access-control", "router", "enabled (default policy: allow new devices)")

        now = now or datetime.now(UTC)
        rules = self.registry.rules()
        until: datetime | None = None
        for device in members:
            await actions.set_device_access(self.client, device.mac, allow=allow)
            self.registry.set_access_state(device.mac, allow)
            mine = [r for r in rules if rule_applies_to(r, device)]
            device_until = next_boundary(mine, now.astimezone()) if mine else None
            if device_until is not None:
                device_until = device_until.astimezone(UTC)
                until = device_until if until is None else min(until, device_until)
            if allow and not mine:
                # nothing schedules this device, so "on" simply returns it to unmanaged
                self.registry.clear_override(device.mac)
            else:
                self.registry.set_override(device.mac, allow=allow, until=device_until, reason=reason)
            self._log(
                "allow" if allow else "block", f"{device.display_name} {device.mac}", f"{kind} {target}"
            )

        return AccessChange(
            target=target,
            kind=kind,
            allow=allow,
            devices=[d for d in (self.registry.get(m.mac) for m in members) if d],
            until=until,
            access_control_enabled_now=enabled_now,
        )

    def blocked_macs(self, extra: list[str] | None = None) -> list[str]:
        """MACs curfew believes are blocked: flagged blocked in the registry or holding a block override.

        This firmware exposes no way to read the router's full block list, so this is the best the
        toolkit can enumerate. `extra` adds MACs the registry no longer tracks, e.g. a device's old
        randomized address after a merge. Returns normalized MACs, in a stable order, deduplicated.
        """
        macs: dict[str, None] = {}
        for d in self.registry.all():
            if d.access_state == "block":
                macs[normalize_mac(d.mac)] = None
        for mac, override in self.registry.overrides().items():
            if not override.allow:
                macs[normalize_mac(mac)] = None
        for mac in extra or []:
            macs[normalize_mac(mac)] = None
        return list(macs)

    async def clear_access_control(self, extra: list[str] | None = None) -> list[str]:
        """Allow every blocked MAC curfew knows about (plus any `extra`), clearing the deny list.

        Also drops the local block overrides, so the watcher does not re-apply them. Returns the
        MACs cleared. Leaves the Access Control feature itself enabled; use set_access_control(False)
        to turn the whole feature off instead.
        """
        cleared: list[str] = []
        for mac in self.blocked_macs(extra):
            await actions.set_device_access(self.client, mac, allow=True)
            self.registry.set_access_state(mac, True)
            self.registry.clear_override(mac)
            self._log("allow", mac, "clear deny list")
            cleared.append(mac)
        return cleared

    async def set_access_control(self, enabled: bool) -> None:
        await actions.set_access_control_enabled(self.client, enabled)
        self._log("access-control", "router", "enabled" if enabled else "disabled")

    # -- schedules -----------------------------------------------------------

    def add_schedule(self, target: str, *, start: str, end: str, days: str = "daily", name: str = "") -> Rule:
        kind, members = self.resolve_target(target)
        parse_hhmm(start), parse_hhmm(end)  # validate
        rule = Rule(
            name=name or f"{target} {start}-{end}",
            kind=kind,
            target=members[0].mac if kind == "device" else target,
            start=start,
            end=end,
            days=parse_days(days),
        )
        saved = self.registry.add_rule(rule)
        self._log("schedule-add", saved.name, f"{kind} {target} {start}-{end} {format_days(saved.days)}")
        return saved

    def add_guest_schedule(
        self, *, start: str, end: str, days: str = "daily", band: str = "both", name: str = ""
    ) -> Rule:
        """Turn the guest network off every day between start and end, and back on after."""
        if band not in GUEST_TARGETS:
            raise ValueError("band must be both, 2.4 or 5")
        parse_hhmm(start), parse_hhmm(end)  # validate
        rule = Rule(
            name=name or f"guest {start}-{end}",
            kind=GUEST_KIND,
            target=band,
            start=start,
            end=end,
            days=parse_days(days),
        )
        saved = self.registry.add_rule(rule)
        self._log(
            "schedule-add", saved.name, f"guest wifi {band} off {start}-{end} {format_days(saved.days)}"
        )
        return saved

    def remove_schedule(self, rule_id: int) -> bool:
        removed = self.registry.remove_rule(rule_id)
        if removed:
            self._log("schedule-remove", str(rule_id), "")
        return removed

    async def apply_schedules(self, *, now: datetime | None = None) -> list[tuple[KnownDevice, bool]]:
        """Bring every managed device to its desired state. Returns the changes made."""
        now = now or datetime.now(UTC)
        rules = [r for r in self.registry.rules() if r.enabled]
        overrides = self.registry.overrides()
        for mac, override in list(overrides.items()):
            if override_expired(override, now):
                self.registry.clear_override(mac)
                del overrides[mac]
        changes: list[tuple[KnownDevice, bool]] = []
        managed = [
            d for d in self.registry.all() if d.mac in overrides or any(rule_applies_to(r, d) for r in rules)
        ]
        if not managed:
            return changes
        access_control_checked = False
        for device in managed:
            wanted = desired_state(device, rules, overrides.get(device.mac), now)
            if wanted is None:
                continue
            current = {"allow": True, "block": False}.get(device.access_state)
            if current == wanted:
                continue
            if not wanted and not access_control_checked:
                if not await actions.get_access_control_enabled(self.client):
                    await actions.set_access_control_enabled(self.client, True)
                    self._log("access-control", "router", "enabled by scheduler")
                access_control_checked = True
            await actions.set_device_access(self.client, device.mac, allow=wanted)
            self.registry.set_access_state(device.mac, wanted)
            self._log("allow" if wanted else "block", f"{device.display_name} {device.mac}", "schedule")
            changes.append((device, wanted))
        return changes

    async def apply_guest_schedules(self, *, now: datetime | None = None) -> list[tuple[Band, bool]]:
        """Switch the guest network per band when a guest schedule window starts or ends.

        Edge-triggered: it acts only when the scheduled state changes from what it last applied, so a
        manual `guest on/off` in between is respected until the next window boundary. On first sight
        of a rule it enforces "off" if the window is already active, but never forces the guest
        network on, so adding a night rule during the day leaves the current state alone.
        """
        now = now or datetime.now(UTC)
        now_local = now.astimezone()
        rules = [r for r in self.registry.rules() if r.enabled]
        changes: list[tuple[Band, bool]] = []
        for band in (Band.GHZ_2_4, Band.GHZ_5):
            key = f"guest_schedule:{band.value}"
            wanted = desired_guest(rules, band, now_local)
            if wanted is None:
                self.registry.clear_state(key)  # no rule covers this band any more
                continue
            wanted_s = "on" if wanted else "off"
            last = self.registry.get_state(key)
            if last == wanted_s:
                continue
            self.registry.set_state(key, wanted_s)
            if last is None and wanted:
                continue  # first sight outside the window: leave the guest network as it is
            current = (await actions.get_wlan_info(self.client, band)).guest_enabled
            if current == wanted:
                continue
            await actions.set_guest_wifi(self.client, band, wanted)
            self._log("guest-wifi", band.value, f"{wanted_s} by schedule")
            changes.append((band, wanted))
        return changes

    async def enforce(self, *, now: datetime | None = None) -> Enforcement:
        """Apply every schedule once: device access and the guest network."""
        devices = await self.apply_schedules(now=now)
        guest = await self.apply_guest_schedules(now=now)
        return Enforcement(devices=devices, guest=guest)

    async def tick(self, *, now: datetime | None = None) -> tuple[ScanDelta, Enforcement]:
        """One watcher cycle: refresh the registry from the router, then enforce schedules."""
        delta = await self.devices.scan()
        return delta, await self.enforce(now=now)

    # -- other writes ----------------------------------------------------------

    async def reboot(self) -> None:
        self._log("reboot", "router", "")
        await actions.reboot(self.client)

    async def set_guest_wifi(self, band: Band, enabled: bool) -> None:
        await actions.set_guest_wifi(self.client, band, enabled)
        self._log("guest-wifi", band.value, "on" if enabled else "off")

    # -- audit log -------------------------------------------------------------

    def _log(self, action: str, target: str, detail: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"{stamp} {action:15} {target}  {detail}".rstrip()
        try:
            self.changes_log.parent.mkdir(parents=True, exist_ok=True)
            with self.changes_log.open("a") as fh:
                fh.write(line + "\n")
        except OSError as err:  # never let logging break a change
            log.warning("could not write %s: %s", self.changes_log, err)
        log.info(line)


def is_mac(text: str) -> bool:
    return normalize_mac(text).count(":") == 5
