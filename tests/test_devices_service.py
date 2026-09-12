"""DeviceService over the fake router plus a temporary registry, and the MCP tools on top."""

from __future__ import annotations

from pathlib import Path

import pytest

from curfew import mcp_server
from curfew.registry import Registry
from curfew.services.devices import AmbiguousDevice, DeviceService, UnknownDevice
from curfew.soap import SoapClient
from curfew.vendor import VendorDb
from tests.conftest import FakeRouter


@pytest.fixture
def service(client: SoapClient, router: FakeRouter, tmp_path: Path) -> DeviceService:
    router.serve_fixture("DeviceInfo:1", "GetAttachDevice2")
    (tmp_path / "oui.csv").write_text(
        "Registry,Assignment,Organization Name,Organization Address\nMA-L,98DAC4,TP-Link,CN\n"
    )
    registry = Registry(tmp_path / "registry.db")
    yield DeviceService(client, registry, VendorDb(tmp_path))  # type: ignore[misc]
    registry.close()


async def test_list_online_merges_registry(service: DeviceService) -> None:
    views = await service.list_online()
    assert len(views) == 3
    assert views[0].vendor == "TP-Link"
    assert views[0].first_seen is not None
    assert views[0].display_name == "Smart Wi-Fi Light Switch"  # router name is Unknown, model wins
    assert views[2].randomized_mac is True  # EA:82:62:00:00:03
    assert views[2].vendor == "(randomized MAC)"
    service.name("98:DA:C4:00:00:01", nickname="Hall switch", owner="house", tags=["iot"])
    views = await service.list_online()
    assert views[0].display_name == "Hall switch" and views[0].owner == "house" and views[0].tags == ["iot"]


async def test_resolve_rules(service: DeviceService) -> None:
    await service.scan()
    assert service.resolve("192.168.1.51").last_name == "HS200"
    assert service.resolve("hs200").mac == "98:DA:C4:00:00:02"
    with pytest.raises(UnknownDevice):
        service.resolve("toaster")
    # two devices get the same nickname? refused
    service.name("HS200", nickname="Kitchen")
    with pytest.raises(ValueError):
        service.name("98:DA:C4:00:00:01", nickname="kitchen")
    # ambiguity: give both the same router name in the registry by renaming through set_identity is not
    # possible, so simulate with two devices sharing last_name via a second scan is not needed here.
    service.name("98:DA:C4:00:00:01", nickname="Hall")
    assert service.resolve("hall").mac == "98:DA:C4:00:00:01"


async def test_history(service: DeviceService) -> None:
    await service.scan()
    known, sessions = service.history("HS200")
    assert known.online and len(sessions) == 1 and sessions[0].ended_at is None


async def test_ambiguous_router_name(service: DeviceService) -> None:
    await service.scan()
    # Both light switches report model "Smart Wi-Fi Light Switch" but names differ; force a clash on last_name
    service.registry._db.execute("UPDATE devices SET last_name='Switch' WHERE mac LIKE '98:DA:C4%'")  # noqa: SLF001
    with pytest.raises(AmbiguousDevice) as err:
        service.resolve("switch")
    assert len(err.value.candidates) == 2


async def test_mcp_tools_end_to_end(service: DeviceService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.state, "devices", lambda: service)
    listed = await mcp_server.list_devices()
    assert listed["count"] == 3 and listed["unnamed_count"] == 2
    assert listed["devices"][1]["display_name"] == "HS200"

    named = await mcp_server.name_device("HS200", nickname="Kitchen", owner="house")
    assert named["display_name"] == "Kitchen" and named["owner"] == "house"

    hist = await mcp_server.device_history("Kitchen")
    assert hist["device"]["mac"] == "98:DA:C4:00:00:02"
    assert hist["sessions"][0]["ongoing"] is True

    fresh = await mcp_server.who_is_new("1h")
    assert len(fresh["devices"]) == 3

    delta = await mcp_server.scan_network()
    assert delta["present"] == 3 and delta["new"] == []

    kd = await mcp_server.known_devices(owner="house")
    assert kd["count"] == 1

    tools = await mcp_server.server.list_tools()
    names = {t.name for t in tools}
    assert {"router_status", "list_devices", "name_device", "system_log", "who_is_new"} <= names
    read_only = {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
    assert "list_devices" in read_only and "name_device" not in read_only


async def test_mcp_lookup_errors_are_tool_errors(
    service: DeviceService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(mcp_server.state, "devices", lambda: service)
    await service.scan()
    with pytest.raises(ToolError, match="no device matches 'toaster'"):
        await mcp_server.device_history("toaster")
    with pytest.raises(ToolError, match="no device matches"):
        await mcp_server.name_device("toaster", nickname="x")
