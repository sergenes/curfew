"""Typed wrappers around individual SOAP actions.

Each function performs exactly one router call and parses the result.
Composition (for example RouterStatus) lives in curfew.services.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from curfew.models import (
    Band,
    Device,
    FirmwareCheck,
    LanInfo,
    LogEntry,
    RouterInfo,
    SystemInfo,
    TrafficPeriod,
    TrafficStats,
    WanInfo,
    WlanInfo,
)
from curfew.soap import SoapClient, SoapError, local_name

DEVICE_INFO = "DeviceInfo:1"
DEVICE_CONFIG = "DeviceConfig:1"
WAN_IP = "WANIPConnection:1"
WAN_ETHERNET = "WANEthernetLinkConfig:1"
LAN_CONFIG = "LANConfigSecurity:1"
WLAN = "WLANConfiguration:1"


# -- DeviceInfo --------------------------------------------------------------


async def get_router_info(client: SoapClient) -> RouterInfo:
    f = (await client.call(DEVICE_INFO, "GetInfo")).fields()
    return RouterInfo(
        model=f.get("ModelName", ""),
        description=f.get("Description", ""),
        serial_number=f.get("SerialNumber", ""),
        firmware=f.get("Firmwareversion", ""),
        hardware=f.get("Hardwareversion", ""),
        device_name=f.get("DeviceName", ""),
    )


async def get_uptime(client: SoapClient) -> str:
    return (await client.call(DEVICE_INFO, "GetSysUpTime")).value("SysUpTime")


async def get_system_info(client: SoapClient) -> SystemInfo:
    f = (await client.call(DEVICE_INFO, "GetSystemInfo")).fields()
    return SystemInfo(
        cpu_utilization_percent=_int(f.get("NewCPUUtilization")),
        memory_total_mb=_int(f.get("NewPhysicalMemory")),
        memory_utilization_percent=_int(f.get("NewMemoryUtilization")),
        flash_total_mb=_int(f.get("NewPhysicalFlash")),
        flash_available_mb=_int(f.get("NewAvailableFlash")),
    )


async def get_attached_devices(client: SoapClient) -> list[Device]:
    resp = await client.call(DEVICE_INFO, "GetAttachDevice2")
    body = resp.body
    if body is None:
        return []
    devices: list[Device] = []
    for node in body.iter():
        if local_name(node.tag) != "Device":
            continue
        f = {local_name(c.tag): (c.text or "").strip() for c in node}
        devices.append(
            Device(
                mac=f.get("MAC", "").upper(),
                ip=f.get("IP", ""),
                name=f.get("Name", ""),
                name_user_set=_bool(f.get("NameUserSet")),
                band=Band.parse(f.get("ConnectionType", "")),
                ssid=f.get("SSID", ""),
                link_speed_mbps=_int(f.get("Linkspeed")),
                signal_percent=_int(f.get("SignalStrength")),
                allowed=f.get("AllowOrBlock", "Allow").lower() != "block",
                schedule=_bool(f.get("Schedule")),
                device_type=f.get("DeviceTypeNameV2", ""),
                model=f.get("DeviceModel", ""),
                upload_mbps=_float(f.get("Upload")),
                download_mbps=_float(f.get("Download")),
            )
        )
    return devices


_LOG_LINE = re.compile(
    r"^\[(?P<category>[^\]]+)\]\s*(?P<message>.*?),\s*(?P<ts>\w{3} \w{3} +\d+ \d\d:\d\d:\d\d \d{4})\s*$"
)


async def get_system_logs(client: SoapClient) -> list[LogEntry]:
    text = (await client.call(DEVICE_INFO, "GetSystemLogs")).value("NewLogDetails")
    entries: list[LogEntry] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _LOG_LINE.match(raw)
        if not m:
            entries.append(LogEntry(timestamp=None, category="", message=raw, raw=raw))
            continue
        try:
            ts: datetime | None = datetime.strptime(re.sub(r" +", " ", m["ts"]), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            ts = None
        category, message = m["category"], m["message"]
        # "[DHCP IP: 192.168.1.5] to MAC address aa:bb..." carries data inside the bracket.
        if ":" in category:
            category, detail = (part.strip() for part in category.split(":", 1))
            if category.endswith(" IP"):
                category, detail = category[:-3], f"IP {detail}"
            message = f"{detail} {message}".strip()
        entries.append(LogEntry(timestamp=ts, category=category, message=message, raw=raw))
    return entries


# -- DeviceConfig ------------------------------------------------------------


async def get_access_control_enabled(client: SoapClient) -> bool:
    return _bool(
        (await client.call(DEVICE_CONFIG, "GetBlockDeviceEnableStatus")).value("NewBlockDeviceEnable")
    )


async def check_firmware(client: SoapClient) -> FirmwareCheck:
    f = (await client.call(DEVICE_CONFIG, "CheckNewFirmware")).fields()
    return FirmwareCheck(
        current_version=f.get("CurrentVersion", ""),
        new_version=f.get("NewVersion", ""),
        release_note=f.get("ReleaseNote", ""),
    )


async def get_traffic_stats(client: SoapClient) -> TrafficStats:
    enabled_resp, stats_resp = await asyncio.gather(
        client.call(DEVICE_CONFIG, "GetTrafficMeterEnabled"),
        client.call(DEVICE_CONFIG, "GetTrafficMeterStatistics"),
    )
    f = stats_resp.fields()

    def period(prefix: str) -> TrafficPeriod:
        up_total, up_avg = _total_avg(f.get(f"New{prefix}Upload"))
        down_total, down_avg = _total_avg(f.get(f"New{prefix}Download"))
        return TrafficPeriod(
            connection_time=_hhmm(f.get(f"New{prefix}ConnectionTime")),
            upload_mb=up_total,
            download_mb=down_total,
            upload_avg_mb=up_avg,
            download_avg_mb=down_avg,
        )

    return TrafficStats(
        enabled=_bool(enabled_resp.value("NewTrafficMeterEnable")),
        today=period("Today"),
        yesterday=period("Yesterday"),
        week=period("Week"),
        month=period("Month"),
        last_month=period("LastMonth"),
    )


# -- WAN / LAN ---------------------------------------------------------------


async def get_wan_info(client: SoapClient) -> WanInfo:
    f = (await client.call(WAN_IP, "GetInfo")).fields()
    try:
        link = (await client.call(WAN_ETHERNET, "GetEthernetLinkStatus")).value("NewEthernetLinkStatus")
    except SoapError:
        link = ""
    return WanInfo(
        enabled=_bool(f.get("NewEnable")),
        connection_type=f.get("NewConnectionType", ""),
        external_ip=f.get("NewExternalIPAddress", ""),
        subnet_mask=f.get("NewSubnetMask", ""),
        gateway=f.get("NewDefaultGateway", ""),
        mac=_mac(f.get("NewMACAddress", "")),
        mtu=_int(f.get("NewMaxMTUSize")),
        dns_servers=f.get("NewDNSServers", "").split(),
        ethernet_link=link,
    )


async def get_lan_info(client: SoapClient) -> LanInfo:
    f = (await client.call(LAN_CONFIG, "GetInfo")).fields()
    return LanInfo(
        ip=f.get("NewLANIP", ""),
        subnet_mask=f.get("NewLANSubnet", ""),
        mac=_mac(f.get("NewLANMACAddress", "")),
        dhcp_enabled=_bool(f.get("NewDHCPEnabled")),
    )


# -- WLAN --------------------------------------------------------------------


async def get_wlan_info(client: SoapClient, band: Band) -> WlanInfo:
    if band is Band.GHZ_2_4:
        info_action, guest_action, guest_net_action = (
            "GetInfo",
            "GetGuestAccessEnabled",
            "GetGuestAccessNetworkInfo",
        )
    elif band is Band.GHZ_5:
        info_action, guest_action, guest_net_action = (
            "Get5GInfo",
            "Get5GGuestAccessEnabled",
            "Get5GGuestAccessNetworkInfo",
        )
    else:
        raise ValueError(f"no wifi info for band {band}")
    info, guest, guest_net = await asyncio.gather(
        client.call(WLAN, info_action),
        client.call(WLAN, guest_action),
        client.call(WLAN, guest_net_action),
    )
    f = info.fields()
    return WlanInfo(
        band=band,
        enabled=_bool(f.get("NewEnable")),
        ssid=f.get("NewSSID", ""),
        broadcast=_bool(f.get("NewSSIDBroadcast")),
        status=f.get("NewStatus", ""),
        channel=f.get("NewChannel", ""),
        region=f.get("NewRegion", ""),
        wireless_mode=f.get("NewWirelessMode", ""),
        security=f.get("NewWPAEncryptionModes", "") or f.get("NewBasicEncryptionModes", ""),
        mac=_mac(f.get("NewWLANMACAddress", "")),
        guest_enabled=_bool(guest.value("NewGuestAccessEnabled")),
        guest_ssid=guest_net.fields().get("NewSSID", ""),
    )


# -- parsing helpers ---------------------------------------------------------


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value.replace(",", "")) if value not in (None, "") else None
    except ValueError:
        return None


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _mac(raw: str) -> str:
    """Router returns LAN/WAN MACs without separators: 941865000001."""
    hexed = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    if len(hexed) != 12:
        return raw
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def _total_avg(value: str | None) -> tuple[float | None, float | None]:
    """Traffic values come as '25,350.10' or as 'total/avg' like '6.19/0.88'."""
    if not value or "--" in value:
        return None, None
    parts = value.replace(",", "").split("/")
    total = _float(parts[0])
    avg = _float(parts[1]) if len(parts) > 1 else None
    return total, avg


def _hhmm(value: str | None) -> timedelta | None:
    if not value or ":" not in value:
        return None
    try:
        hours, minutes = value.split(":")
        return timedelta(hours=int(hours), minutes=int(minutes))
    except ValueError:
        return None


# -- writes (all wrapped in configuration mode) -------------------------------


async def set_access_control_enabled(client: SoapClient, enabled: bool) -> None:
    """Turn the router's Access Control feature on or off. Per-device blocks only bite while it is on."""
    async with client.config_mode():
        await client.call(
            DEVICE_CONFIG, "SetBlockDeviceEnable", {"NewBlockDeviceEnable": "1" if enabled else "0"}
        )


async def set_device_access(client: SoapClient, mac: str, *, allow: bool) -> None:
    """Allow or block one MAC address through Access Control."""
    async with client.config_mode():
        await client.call(
            DEVICE_CONFIG,
            "SetBlockDeviceByMAC",
            {"NewAllowOrBlock": "Allow" if allow else "Block", "NewMACAddress": mac.upper()},
        )


async def reboot(client: SoapClient) -> None:
    """Reboot the router. The session dies with it, so ConfigurationFinished is expected to fail."""
    async with client.config_mode():
        await client.call(DEVICE_CONFIG, "Reboot")


async def set_guest_wifi(client: SoapClient, band: Band, enabled: bool) -> None:
    action = {Band.GHZ_2_4: "SetGuestAccessEnabled", Band.GHZ_5: "Set5GGuestAccessEnabled"}.get(band)
    if action is None:
        raise ValueError(f"no guest network for band {band}")
    async with client.config_mode():
        await client.call(WLAN, action, {"NewGuestAccessEnabled": "1" if enabled else "0"})
