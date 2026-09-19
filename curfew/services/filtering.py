"""DNS filter policy: manage per-scope modes and lists, and resolve one device's effective policy.

A scope is where a rule applies, most specific first: a single device, then its owner, then a group
(tag) it belongs to, then the global default. The resolver walks that order and uses the first scope
that has a mode set, so you can block a whole group and still exempt one device.

This service only reads and writes the registry. The daemon in curfew/services/dns.py calls
policy_for_ip on every lookup; the CLI and MCP call the management methods.
"""

from __future__ import annotations

from curfew.dnsfilter import Mode, Policy
from curfew.models import KnownDevice
from curfew.registry import Registry
from curfew.vendor import normalize_mac

SCOPE_ORDER = ("device", "owner", "group", "global")


class FilterService:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    # -- resolving a target string to a scope ----------------------------

    def resolve_scope(self, target: str) -> tuple[str, str, str]:
        """(scope_kind, scope_value, label). 'all'/'' is global; else group, then owner, then device."""
        t = target.strip()
        if not t or t.lower() in {"all", "global", "everyone"}:
            return "global", "", "everyone"
        if any(name.lower() == t.lower() for name in self.registry.groups()):
            tag = next(name for name in self.registry.groups() if name.lower() == t.lower())
            return "group", tag, f"group {tag}"
        if self.registry.by_owner(t):
            return "owner", t, f"owner {t}"
        candidates = self.registry.resolve(t)
        if not candidates:
            raise LookupError(f"no group, owner or device matches {target!r}")
        if len(candidates) > 1:
            names = ", ".join(f"{c.display_name} ({c.mac})" for c in candidates)
            raise ValueError(f"{target!r} matches several devices: {names}. Use the MAC address.")
        dev = candidates[0]
        return "device", dev.mac, f"device {dev.display_name}"

    # -- management ------------------------------------------------------

    def set_mode(self, target: str, mode: str) -> tuple[str, str]:
        if mode not in {m.value for m in Mode}:
            raise ValueError("mode must be off, blacklist or whitelist")
        kind, value, label = self.resolve_scope(target)
        self.registry.set_filter_mode(kind, value, mode)
        return label, mode

    def add(self, target: str, pattern: str, *, block: bool) -> tuple[str, str]:
        kind, value, label = self.resolve_scope(target)
        self.registry.add_filter_rule(kind, value, "block" if block else "allow", pattern.strip().lower())
        return label, pattern.strip().lower()

    def remove(self, target: str, pattern: str, *, block: bool) -> bool:
        kind, value, _ = self.resolve_scope(target)
        return self.registry.remove_filter_rule(
            kind, value, "block" if block else "allow", pattern.strip().lower()
        )

    def rules(self, target: str | None = None) -> list[tuple[str, str, str, str]]:
        if target is None:
            return self.registry.filter_rules()
        kind, value, _ = self.resolve_scope(target)
        return self.registry.filter_rules(kind, value)

    def modes(self) -> list[tuple[str, str, str]]:
        return self.registry.filter_modes()

    # -- policy resolution (hot path for the daemon) ---------------------

    def _policy_for_scope(self, kind: str, value: str, label: str) -> Policy | None:
        mode = self.registry.get_filter_mode(kind, value)
        if mode == "off":
            return None
        block = {r[3] for r in self.registry.filter_rules(kind, value) if r[2] == "block"}
        allow = {r[3] for r in self.registry.filter_rules(kind, value) if r[2] == "allow"}
        return Policy(mode=Mode(mode), block=block, allow=allow, scope=label)

    def policy_for_device(self, device: KnownDevice) -> Policy:
        """The most specific scope with a mode set wins: device, then owner, then a group, then global."""
        p = self._policy_for_scope("device", device.mac, f"device {device.display_name}")
        if p:
            return p
        if device.owner:
            p = self._policy_for_scope("owner", device.owner, f"owner {device.owner}")
            if p:
                return p
        for tag in device.tags:
            p = self._policy_for_scope("group", tag, f"group {tag}")
            if p:
                return p
        return self._policy_for_scope("global", "", "everyone") or Policy()

    def policy_for_ip(self, ip: str) -> Policy:
        """Map a querying client IP to a device, then to its policy. Unknown clients get the global one."""
        for d in self.registry.all():
            if d.last_ip == ip:
                return self.policy_for_device(d)
        return self._policy_for_scope("global", "", "everyone") or Policy()

    def forget_mac(self, mac: str) -> None:
        """Drop device-scoped rules/mode for a MAC (used when a device is merged or forgotten)."""
        mac = normalize_mac(mac)
        self.registry.set_filter_mode("device", mac, "off")
        for _, _, list_kind, pattern in self.registry.filter_rules("device", mac):
            self.registry.remove_filter_rule("device", mac, list_kind, pattern)
