"""Changes to the router: access on/off for devices, owners and groups, schedules, reboot, guest wifi.

Every change is appended to <data_dir>/changes.log.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from curfew import actions
from curfew.models import AccessChange, Band, KnownDevice, Rule
from curfew.registry import Registry
from curfew.scheduler import (
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
