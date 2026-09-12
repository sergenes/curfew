"""EclipseService and the pause MCP tools over the fake router and a temp registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from curfew import mcp_server
from curfew.config import Settings
from curfew.pause import PauseError
from curfew.registry import Registry
from curfew.services.eclipse import EclipseService
from curfew.soap import SoapClient
from tests.conftest import FakeRouter

SWITCH_A = "98:DA:C4:00:00:01"
SWITCH_B = "98:DA:C4:00:00:02"


@pytest.fixture
async def ecl(client: SoapClient, router: FakeRouter, tmp_path: Path) -> EclipseService:
    router.serve_fixture("DeviceInfo:1", "GetAttachDevice2")
    settings = Settings(host="router.test", user="admin", password="pw", data_dir=tmp_path)
    registry = Registry(tmp_path / "registry.db")
    service = EclipseService(client, registry, settings)
    await service.devices.scan()
    registry.set_identity(SWITCH_A, nickname="Hall", tags=["kids"])
    registry.set_identity(SWITCH_B, nickname="Kitchen", tags=["kids"])
    yield service  # type: ignore[misc]
    registry.close()


async def test_pause_records_target_with_current_ip(ecl: EclipseService) -> None:
    target = await ecl.pause("Hall", reason="dinner")
    assert target.mac == SWITCH_A
    assert target.ip == "192.168.1.202"  # from the fixture
    assert target.reason == "dinner"
    paused = ecl.list_paused()
    assert [p.mac for p in paused] == [SWITCH_A]
    assert paused[0].display_name == "Hall"


async def test_pause_refuses_when_target_ip_is_the_router(ecl: EclipseService) -> None:
    # Give the service a host equal to the Hall switch's fixture IP, so pausing it is refused.
    guarded = EclipseService(
        ecl.client, ecl.registry, ecl.settings.__class__(**{**ecl.settings.__dict__, "host": "192.168.1.202"})
    )
    with pytest.raises(PauseError, match="router"):
        await guarded.pause("Hall")


async def test_resume_and_resume_all(ecl: EclipseService) -> None:
    await ecl.pause("Hall")
    await ecl.pause("Kitchen")
    assert len(ecl.list_paused()) == 2
    assert ecl.resume("Hall") is True
    assert [p.mac for p in ecl.list_paused()] == [SWITCH_B]
    assert ecl.resume_all() == 1
    assert ecl.list_paused() == []


async def test_enforcing_reflects_heartbeat(ecl: EclipseService) -> None:
    from curfew.pause import write_heartbeat

    assert ecl.enforcing() is False
    write_heartbeat(ecl.settings.data_dir)
    assert ecl.enforcing() is True


async def test_mcp_pause_group_and_list(ecl: EclipseService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.state, "eclipse", lambda: ecl)
    monkeypatch.setattr(mcp_server.state, "devices", lambda: ecl.devices)

    res = await mcp_server.pause_device("kids", reason="homework")
    assert res["kind"] == "group"
    assert {d["mac"] for d in res["paused"]} == {SWITCH_A, SWITCH_B}
    assert res["enforcing"] is False
    assert "not running" in res["note"]

    listed = await mcp_server.list_paused()
    assert {p["mac"] for p in listed["paused"]} == {SWITCH_A, SWITCH_B}

    resumed = await mcp_server.resume_device(all_devices=True)
    assert resumed["resumed"] == 2
    assert (await mcp_server.list_paused())["paused"] == []


async def test_mcp_pause_unknown_is_tool_error(ecl: EclipseService, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(mcp_server.state, "eclipse", lambda: ecl)
    with pytest.raises(ToolError, match="no device matches 'toaster'"):
        await mcp_server.pause_device("toaster")
