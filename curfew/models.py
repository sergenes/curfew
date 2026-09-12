"""Typed views of what the router returns."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field


class Band(StrEnum):
    WIRED = "wired"
    GHZ_2_4 = "2.4GHz"
    GHZ_5 = "5GHz"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str) -> Band:
        value = raw.strip().lower()
        if value == "wired":
            return cls.WIRED
        if value.startswith("2.4"):
            return cls.GHZ_2_4
        if value.startswith("5"):
            return cls.GHZ_5
        return cls.UNKNOWN


class Device(BaseModel):
    """One entry from GetAttachDevice2."""

    mac: str
    ip: str
    name: str
    name_user_set: bool = False
    band: Band = Band.UNKNOWN
    ssid: str = ""
    link_speed_mbps: int | None = None
    signal_percent: int | None = None
    allowed: bool = True
    schedule: bool = False
    device_type: str = ""
    model: str = ""
    upload_mbps: float | None = None
    download_mbps: float | None = None

    @property
    def is_known(self) -> bool:
        return self.name.lower() not in {"", "unknown", "<unknown>"}


class RouterInfo(BaseModel):
    model: str
    description: str = ""
    serial_number: str
    firmware: str
    hardware: str = ""
    device_name: str = ""


class SystemInfo(BaseModel):
    cpu_utilization_percent: int | None = None
    memory_total_mb: int | None = None
    memory_utilization_percent: int | None = None
    flash_total_mb: int | None = None
    flash_available_mb: int | None = None


class WanInfo(BaseModel):
    enabled: bool
    connection_type: str
    external_ip: str
    subnet_mask: str = ""
    gateway: str = ""
    mac: str = ""
    mtu: int | None = None
    dns_servers: list[str] = Field(default_factory=list)
    ethernet_link: str = ""


class LanInfo(BaseModel):
    ip: str
    subnet_mask: str
    mac: str = ""
    dhcp_enabled: bool = True


class WlanInfo(BaseModel):
    band: Band
    enabled: bool
    ssid: str
    broadcast: bool
    status: str
    channel: str
    region: str = ""
    wireless_mode: str = ""
    security: str = ""
    mac: str = ""
    guest_enabled: bool | None = None
    guest_ssid: str = ""


class TrafficPeriod(BaseModel):
    connection_time: timedelta | None = None
    upload_mb: float | None = None
    download_mb: float | None = None
    upload_avg_mb: float | None = None
    download_avg_mb: float | None = None


class TrafficStats(BaseModel):
    enabled: bool
    today: TrafficPeriod
    yesterday: TrafficPeriod
    week: TrafficPeriod
    month: TrafficPeriod
    last_month: TrafficPeriod


class LogEntry(BaseModel):
    timestamp: datetime | None
    category: str
    message: str
    raw: str


class FirmwareCheck(BaseModel):
    current_version: str
    new_version: str = ""
    release_note: str = ""

    @property
    def update_available(self) -> bool:
        return bool(self.new_version) and self.new_version != self.current_version


class KnownDevice(BaseModel):
    """What the local registry remembers about a MAC address."""

    mac: str
    first_seen: datetime
    last_seen: datetime
    last_ip: str = ""
    last_name: str = ""
    last_band: str = ""
    last_type: str = ""
    last_model: str = ""
    vendor: str = ""
    nickname: str = ""
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    online: bool = False
    randomized_mac: bool = False
    access_state: str = ""  # "allow", "block" or "" when never observed

    @property
    def display_name(self) -> str:
        if self.nickname:
            return self.nickname
        if self.last_name.lower() not in {"", "unknown", "<unknown>"}:
            return self.last_name
        if self.last_model:
            return self.last_model
        return self.vendor or "unknown"


class Rule(BaseModel):
    """A recurring window during which a target is blocked. Times are local wall clock."""

    id: int | None = None
    name: str
    kind: str  # "group", "owner" or "device"
    target: str  # tag name, owner name, or MAC
    start: str  # "21:00"
    end: str  # "07:00"; earlier than start means the window crosses midnight
    days: list[int] = Field(default_factory=lambda: list(range(7)))  # 0 = Monday
    enabled: bool = True


class AccessOverride(BaseModel):
    """A manual on/off that wins over schedules until `until` (None: until flipped back)."""

    mac: str
    allow: bool
    set_at: datetime
    until: datetime | None = None
    reason: str = ""


class PauseTarget(BaseModel):
    """A device currently held in Eclipse Pause (ARP interception)."""

    mac: str
    ip: str
    since: datetime
    reason: str = ""
    display_name: str = ""


class AccessChange(BaseModel):
    """Result of an access on/off request."""

    target: str
    kind: str  # "group", "owner" or "device"
    allow: bool
    devices: list[KnownDevice]
    until: datetime | None = None
    access_control_enabled_now: bool = False


class PresenceSession(BaseModel):
    """One continuous stretch of a device being attached to the router."""

    mac: str
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None
    ip: str = ""
    band: str = ""

    @property
    def duration(self) -> timedelta:
        return (self.ended_at or self.last_seen_at) - self.started_at


class ScanDelta(BaseModel):
    """What changed between the previous scan and this one."""

    scanned_at: datetime
    present: int
    new: list[KnownDevice] = Field(default_factory=list)
    returned: list[KnownDevice] = Field(default_factory=list)
    left: list[KnownDevice] = Field(default_factory=list)


class DeviceView(Device):
    """A live device merged with what the registry knows about it."""

    nickname: str = ""
    owner: str = ""
    tags: list[str] = Field(default_factory=list)
    vendor: str = ""
    first_seen: datetime | None = None
    randomized_mac: bool = False

    @property
    def display_name(self) -> str:
        if self.nickname:
            return self.nickname
        if self.is_known:
            return self.name
        return self.model or self.vendor or "unknown"


class RouterStatus(BaseModel):
    info: RouterInfo
    uptime: str
    system: SystemInfo
    wan: WanInfo
    lan: LanInfo
    access_control_enabled: bool
    device_count: int | None = None
