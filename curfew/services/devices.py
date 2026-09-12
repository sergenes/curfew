"""Device presence: live listing merged with the registry, plus name resolution."""

from __future__ import annotations

from datetime import UTC, datetime

from curfew import actions
from curfew.models import DeviceView, KnownDevice, PresenceSession, ScanDelta
from curfew.registry import Registry
from curfew.soap import SoapClient
from curfew.vendor import VendorDb, is_randomized


class UnknownDevice(LookupError):
    def __init__(self, query: str) -> None:
        super().__init__(f"no device matches {query!r}; use a MAC, nickname, router name or IP")
        self.query = query


class AmbiguousDevice(LookupError):
    def __init__(self, query: str, candidates: list[KnownDevice]) -> None:
        names = ", ".join(f"{c.display_name} ({c.mac})" for c in candidates)
        super().__init__(f"{query!r} matches several devices: {names}. Use the MAC address.")
        self.query = query
        self.candidates = candidates


class DeviceService:
    def __init__(self, client: SoapClient, registry: Registry, vendors: VendorDb | None = None) -> None:
        self.client = client
        self.registry = registry
        self.vendors = vendors

    async def scan(self) -> ScanDelta:
        """Fetch the live listing and fold it into the registry."""
        devices = await actions.get_attached_devices(self.client)
        lookup = self.vendors.lookup if self.vendors else None
        return self.registry.record_scan(devices, vendor_lookup=lookup)

    async def list_online(self) -> list[DeviceView]:
        """Live devices enriched with registry knowledge. Also records a scan."""
        devices = await actions.get_attached_devices(self.client)
        lookup = self.vendors.lookup if self.vendors else None
        self.registry.record_scan(devices, vendor_lookup=lookup)
        known = {k.mac: k for k in self.registry.all()}
        views: list[DeviceView] = []
        for d in devices:
            k = known.get(d.mac)
            views.append(
                DeviceView(
                    **d.model_dump(),
                    nickname=k.nickname if k else "",
                    owner=k.owner if k else "",
                    tags=k.tags if k else [],
                    vendor=k.vendor if k else "",
                    first_seen=k.first_seen if k else None,
                    randomized_mac=is_randomized(d.mac),
                )
            )
        return views

    def new_since(self, since: datetime) -> list[KnownDevice]:
        return self.registry.new_since(since)

    def resolve(self, query: str) -> KnownDevice:
        """One device for a MAC, nickname, router name or IP, or raise."""
        candidates = self.registry.resolve(query)
        if not candidates:
            raise UnknownDevice(query)
        if len(candidates) > 1:
            # Several devices may share a router name like "iPhone"; a nickname is unique by convention.
            nick = [c for c in candidates if c.nickname.lower() == query.strip().lower()]
            if len(nick) == 1:
                return nick[0]
            raise AmbiguousDevice(query, candidates)
        return candidates[0]

    def name(
        self,
        query: str,
        *,
        nickname: str | None = None,
        owner: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
    ) -> KnownDevice:
        device = self.resolve(query)
        if nickname:
            clash = [c for c in self.registry.resolve(nickname) if c.mac != device.mac and c.nickname]
            if clash:
                raise ValueError(f"nickname {nickname!r} is already used by {clash[0].mac}")
        return self.registry.set_identity(device.mac, nickname=nickname, owner=owner, tags=tags, notes=notes)

    def history(self, query: str, *, limit: int = 20) -> tuple[KnownDevice, list[PresenceSession]]:
        device = self.resolve(query)
        return device, self.registry.sessions(device.mac, limit=limit)


def humanize(dt: datetime | None, *, now: datetime | None = None) -> str:
    """'3 d ago', '2 h ago', 'just now'."""
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    seconds = int((now - dt).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"
