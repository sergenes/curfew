"""Parsing of real (sanitized) router responses into models."""

from __future__ import annotations

from datetime import datetime, timedelta

from curfew import actions
from curfew.models import Band
from curfew.services import status as status_service
from curfew.soap import SoapClient
from tests.conftest import FakeRouter


async def test_router_info(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceInfo:1", "GetInfo")
    info = await actions.get_router_info(client)
    assert info.model == "CAX80"
    assert info.firmware == "V5.1.1.8"
    assert info.description.startswith("DOCSIS 3.1")


async def test_system_info(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceInfo:1", "GetSystemInfo")
    s = await actions.get_system_info(client)
    assert s.memory_total_mb == 768
    assert s.memory_utilization_percent == 50


async def test_attached_devices(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceInfo:1", "GetAttachDevice2")
    devs = await actions.get_attached_devices(client)
    assert len(devs) == 3
    first = devs[0]
    assert first.mac == "98:DA:C4:00:00:01"
    assert first.ip == "192.168.1.202"
    assert first.band is Band.GHZ_2_4
    assert first.allowed is True
    assert first.schedule is False
    assert first.device_type == "IoT (Generic)"
    assert first.model == "Smart Wi-Fi Light Switch"
    assert first.is_known is False  # name is "Unknown"
    assert devs[1].name == "HS200"
    assert devs[1].is_known is True


async def test_system_logs(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceInfo:1", "GetSystemLogs")
    entries = await actions.get_system_logs(client)
    assert len(entries) == 8
    dhcp = entries[0]
    assert dhcp.category == "DHCP"
    assert dhcp.message == "IP 192.168.1.186 to MAC address f0:03:8c:00:00:04"
    assert dhcp.timestamp == datetime(2026, 9, 12, 12, 36, 15)
    ac = entries[1]
    assert ac.category == "Access Control"
    assert ac.message.startswith("Device Unknown with MAC address 4A:AB:D9:00:00:05")


async def test_traffic_stats(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceConfig:1", "GetTrafficMeterEnabled")
    router.serve_fixture("DeviceConfig:1", "GetTrafficMeterStatistics")
    s = await actions.get_traffic_stats(client)
    assert s.enabled is False
    assert s.today.download_mb == 0.0
    assert s.today.connection_time == timedelta(0)
    assert s.week.upload_avg_mb == 0.0


async def test_wan_and_lan(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("WANIPConnection:1", "GetInfo")
    router.serve_fixture("WANEthernetLinkConfig:1", "GetEthernetLinkStatus")
    router.serve_fixture("LANConfigSecurity:1", "GetInfo")
    wan = await actions.get_wan_info(client)
    assert wan.external_ip == "203.0.113.10"
    assert wan.dns_servers == ["198.51.100.45", "198.51.100.46"]
    assert wan.mac == "94:18:65:00:00:0C"
    assert wan.ethernet_link == "Up"
    lan = await actions.get_lan_info(client)
    assert lan.ip == "192.168.1.1"
    assert lan.dhcp_enabled is True


async def test_wan_survives_missing_link_action(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("WANIPConnection:1", "GetInfo")  # no GetEthernetLinkStatus served -> 501
    wan = await actions.get_wan_info(client)
    assert wan.ethernet_link == ""


async def test_wifi_both_bands(client: SoapClient, router: FakeRouter) -> None:
    for action in (
        "GetInfo",
        "Get5GInfo",
        "GetGuestAccessEnabled",
        "Get5GGuestAccessEnabled",
        "GetGuestAccessNetworkInfo",
        "Get5GGuestAccessNetworkInfo",
    ):
        router.serve_fixture("WLANConfiguration:1", action)
    nets = await status_service.get_wifi(client)
    assert [n.band for n in nets] == [Band.GHZ_2_4, Band.GHZ_5]
    assert nets[0].ssid == "Home-2.4"
    assert nets[1].channel == "44"
    assert nets[1].guest_enabled is False
    assert nets[1].guest_ssid == "NETGEAR-5G-Guest"
    assert nets[0].security == "WPA2-Personal"


async def test_firmware_check(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceConfig:1", "CheckNewFirmware")
    f = await actions.check_firmware(client)
    assert f.current_version == "5.1.1.8"
    assert f.update_available is False


async def test_status_composes_everything(client: SoapClient, router: FakeRouter) -> None:
    for service, action in (
        ("DeviceInfo:1", "GetInfo"),
        ("DeviceInfo:1", "GetSysUpTime"),
        ("DeviceInfo:1", "GetSystemInfo"),
        ("DeviceInfo:1", "GetAttachDevice2"),
        ("DeviceConfig:1", "GetBlockDeviceEnableStatus"),
        ("WANIPConnection:1", "GetInfo"),
        ("WANEthernetLinkConfig:1", "GetEthernetLinkStatus"),
        ("LANConfigSecurity:1", "GetInfo"),
    ):
        router.serve_fixture(service, action)
    s = await status_service.get_status(client)
    assert s.info.model == "CAX80"
    assert s.uptime == "4 days 14:57:40"
    assert s.access_control_enabled is False
    assert s.device_count == 3
    assert router.calls.count("DeviceConfig:1#SOAPLogin") == 1
