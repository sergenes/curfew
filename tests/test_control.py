"""ControlService against the fake router: group resolution, access on/off, schedules, audit log."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from curfew.models import Band
from curfew.registry import Registry
from curfew.services.control import ControlService, GuestWifiUnconfirmed
from curfew.services.devices import UnknownDevice
from curfew.soap import SoapClient
from tests.conftest import FakeRouter, code_only, fixture_text

NY = ZoneInfo("America/New_York")
SWITCH_A = "98:DA:C4:00:00:01"
SWITCH_B = "98:DA:C4:00:00:02"
PHONE = "EA:82:62:00:00:03"


@pytest.fixture
async def ctl(client: SoapClient, router: FakeRouter, tmp_path: Path) -> ControlService:
    router.serve_fixture("DeviceInfo:1", "GetAttachDevice2")
    router.serve_fixture("DeviceConfig:1", "GetBlockDeviceEnableStatus")  # reports 0 = off
    for action in (
        "ConfigurationStarted",
        "ConfigurationFinished",
        "SetBlockDeviceEnable",
        "SetBlockDeviceByMAC",
        "Reboot",
    ):
        router.serve("DeviceConfig:1", action, code_only("000"))
    for action in ("SetGuestAccessEnabled", "Set5GGuestAccessEnabled"):
        router.serve("WLANConfiguration:1", action, code_only("000"))
    registry = Registry(tmp_path / "registry.db")
    service = ControlService(client, registry, tmp_path)
    service.guest_confirm_poll_s = 0.0  # re-read the guest state without pausing in tests
    service.guest_settle_s = 0.0  # an answer still showing the old state means the switch was lost
    await service.devices.scan()
    registry.set_identity(SWITCH_A, nickname="Hall", tags=["kids"], owner="Sam")
    registry.set_identity(SWITCH_B, nickname="Kitchen", tags=["kids"], owner="Alex")
    registry.set_identity(PHONE, nickname="Dad phone", tags=["parents"], owner="Dad")
    yield service  # type: ignore[misc]
    registry.close()


def blocks(router: FakeRouter) -> list[str]:
    return [c for c in router.calls if c.endswith("SetBlockDeviceByMAC")]


async def test_target_resolution(ctl: ControlService) -> None:
    kind, members = ctl.resolve_target("kids")
    assert kind == "group" and {m.mac for m in members} == {SWITCH_A, SWITCH_B}
    kind, members = ctl.resolve_target("sam")
    assert kind == "owner" and [m.mac for m in members] == [SWITCH_A]
    kind, members = ctl.resolve_target("Dad phone")
    assert kind == "device" and members[0].mac == PHONE
    with pytest.raises(UnknownDevice):
        ctl.resolve_target("nobody")


async def test_group_off_enables_access_control_and_blocks_each_member(
    ctl: ControlService, router: FakeRouter
) -> None:
    change = await ctl.set_access("kids", allow=False, reason="dinner")
    assert change.kind == "group" and change.allow is False and change.access_control_enabled_now is True
    assert {d.mac for d in change.devices} == {SWITCH_A, SWITCH_B}
    assert "DeviceConfig:1#SetBlockDeviceEnable" in router.calls
    assert len(blocks(router)) == 2
    assert ctl.registry.get(SWITCH_A).access_state == "block"  # type: ignore[union-attr]
    assert ctl.registry.overrides()[SWITCH_A].allow is False
    assert ctl.registry.overrides()[SWITCH_A].until is None  # sticky: no schedule involved
    log = ctl.changes_log.read_text()
    assert "access-control" in log and "block" in log and "group kids" in log and "Hall" in log
    # config mode brackets every write
    started = router.calls.count("DeviceConfig:1#ConfigurationStarted")
    finished = router.calls.count("DeviceConfig:1#ConfigurationFinished")
    assert started == finished == 3  # enable + 2 blocks


async def test_on_does_not_touch_access_control(ctl: ControlService, router: FakeRouter) -> None:
    change = await ctl.set_access("Dad phone", allow=True)
    assert change.kind == "device" and change.access_control_enabled_now is False
    assert "DeviceConfig:1#SetBlockDeviceEnable" not in router.calls
    assert len(blocks(router)) == 1


async def test_manual_change_lasts_until_next_schedule_boundary(ctl: ControlService) -> None:
    ctl.add_schedule("kids", start="21:00", end="07:00", name="bedtime")
    change = await ctl.set_access("kids", allow=False)
    assert change.until is not None and change.until > datetime.now(UTC)
    assert ctl.registry.overrides()[SWITCH_A].until == change.until


async def test_apply_schedules_blocks_and_releases(ctl: ControlService, router: FakeRouter) -> None:
    rule = ctl.add_schedule("kids", start="21:00", end="07:00", days="daily", name="bedtime")
    assert rule.id is not None and rule.kind == "group"
    night = datetime(2026, 9, 13, 2, 0, tzinfo=NY).astimezone(UTC)
    changes = await ctl.apply_schedules(now=night)
    assert {d.mac for d, allow in changes} == {SWITCH_A, SWITCH_B} and all(not allow for _, allow in changes)
    assert "DeviceConfig:1#SetBlockDeviceEnable" in router.calls
    # idempotent: nothing changes on the next tick
    assert await ctl.apply_schedules(now=night) == []
    morning = datetime(2026, 9, 13, 8, 0, tzinfo=NY).astimezone(UTC)
    changes = await ctl.apply_schedules(now=morning)
    assert {d.mac for d, allow in changes} == {SWITCH_A, SWITCH_B} and all(allow for _, allow in changes)
    assert ctl.registry.get(PHONE).access_state == "allow"  # type: ignore[union-attr]
    assert ctl.remove_schedule(rule.id) is True
    assert ctl.remove_schedule(rule.id) is False


async def test_manual_override_wins_over_schedule_then_expires(ctl: ControlService) -> None:
    ctl.add_schedule("kids", start="21:00", end="07:00", name="bedtime")
    night = datetime(2026, 9, 13, 2, 0, tzinfo=NY).astimezone(UTC)
    await ctl.apply_schedules(now=night)  # blocked by schedule
    # parent says "kids on" at 2am: allowed until 07:00
    change = await ctl.set_access("kids", allow=True, now=night)
    assert all(d.access_state == "allow" for d in change.devices)
    assert change.until == datetime(2026, 9, 13, 7, 0, tzinfo=NY)  # end of tonight's window
    assert await ctl.apply_schedules(now=night) == []  # override holds
    # at 07:00 the override expires; the window also ends, so the devices simply stay allowed
    morning = datetime(2026, 9, 13, 7, 0, tzinfo=NY).astimezone(UTC)
    assert await ctl.apply_schedules(now=morning) == []
    assert ctl.registry.overrides() == {}
    # next night the schedule is back in charge
    next_night = datetime(2026, 9, 13, 22, 0, tzinfo=NY).astimezone(UTC)
    changes = await ctl.apply_schedules(now=next_night)
    assert {d.mac for d, allow in changes} == {SWITCH_A, SWITCH_B} and all(not allow for _, allow in changes)


async def test_reboot_and_guest(ctl: ControlService, router: FakeRouter) -> None:
    GuestWifi(router, on=False)
    [switch] = await ctl.set_guest_wifi([Band.GHZ_5], True)
    assert "WLANConfiguration:1#Set5GGuestAccessEnabled" in router.calls
    assert switch.enabled and switch.sent == 1
    await ctl.reboot()
    assert "DeviceConfig:1#Reboot" in router.calls
    log = ctl.changes_log.read_text()
    assert "guest-wifi" in log and "reboot" in log


def _record_mac_calls(router: FakeRouter) -> list[tuple[str, str]]:
    """Capture (mac, Allow|Block) for each SetBlockDeviceByMAC, so tests can assert what was sent."""
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        mac = re.search(r"<NewMACAddress>([^<]*)", body)
        allow = re.search(r"<NewAllowOrBlock>([^<]*)", body)
        calls.append((mac.group(1) if mac else "", allow.group(1) if allow else ""))
        return httpx.Response(200, text=code_only("000"))

    router.responses["DeviceConfig:1#SetBlockDeviceByMAC"] = handler
    return calls


async def test_clear_access_control_unblocks_known_and_orphan(
    ctl: ControlService, router: FakeRouter
) -> None:
    await ctl.set_access("kids", allow=False)  # block SWITCH_A and SWITCH_B, set block overrides
    assert ctl.registry.get(SWITCH_A).access_state == "block"  # type: ignore[union-attr]

    calls = _record_mac_calls(router)
    orphan = "CE:46:A0:89:F4:6F"  # an old randomized MAC the registry no longer tracks
    cleared = await ctl.clear_access_control(extra=[orphan.lower()])

    assert set(cleared) == {SWITCH_A, SWITCH_B, orphan}
    assert {mac for mac, _ in calls} == {SWITCH_A, SWITCH_B, orphan}
    assert all(allow == "Allow" for _, allow in calls)
    # local state no longer holds a block, so the watcher will not re-apply one
    assert ctl.registry.overrides() == {}
    assert ctl.registry.get(SWITCH_A).access_state == "allow"  # type: ignore[union-attr]
    assert "clear deny list" in ctl.changes_log.read_text()


async def test_clear_access_control_with_nothing_blocked(ctl: ControlService) -> None:
    assert ctl.blocked_macs() == []
    assert await ctl.clear_access_control() == []


# -- watcher tick and guest-network schedules ------------------------------------


async def test_tick_scans_then_enforces_schedules(ctl: ControlService, router: FakeRouter) -> None:
    # Regression: the watcher used to scan without ever applying schedules, so bedtime never fired.
    ctl.add_schedule("kids", start="21:00", end="07:00", days="daily", name="bedtime")
    night = datetime(2026, 9, 13, 2, 0, tzinfo=NY).astimezone(UTC)
    delta, result = await ctl.tick(now=night)
    assert delta.present > 0  # it did scan
    assert {d.mac for d, allow in result.devices} == {SWITCH_A, SWITCH_B}
    assert all(not allow for _, allow in result.devices)


class GuestWifi:
    """A stateful stand-in for the router's guest network on both bands.

    Like the real router, it applies a switch the moment it gets it, then stops answering for
    `outage` requests while its radios restart; anything sent in that time is lost.
    """

    def __init__(self, router: FakeRouter, *, on: bool, outage: int = 0) -> None:
        self.router = router
        self.state = {"2.4": on, "5": on}
        self.writes: list[tuple[str, bool]] = []
        self.outage = outage
        self.ignore = 0  # accept but drop this many upcoming switches
        self.lag = 0  # after a switch, keep reporting the old state for this many reads
        self._pending: dict[str, bool] = {}
        for band, get_action, set_action in (
            ("2.4", "GetGuestAccessEnabled", "SetGuestAccessEnabled"),
            ("5", "Get5GGuestAccessEnabled", "Set5GGuestAccessEnabled"),
        ):
            router.responses[f"WLANConfiguration:1#{get_action}"] = self._getter(band, get_action)
            router.responses[f"WLANConfiguration:1#{set_action}"] = self._setter(band)
        for action in ("GetInfo", "Get5GInfo", "GetGuestAccessNetworkInfo", "Get5GGuestAccessNetworkInfo"):
            router.serve_fixture("WLANConfiguration:1", action)

    def _getter(self, band: str, action: str) -> Any:
        template = fixture_text(f"WLANConfiguration_{action}")

        def handler(_req: httpx.Request) -> httpx.Response:
            if band in self._pending:
                if self._lag_left:
                    self._lag_left -= 1
                else:
                    self.state[band] = self._pending.pop(band)
            value = "1" if self.state[band] else "0"
            body = re.sub(r"<NewGuestAccessEnabled>\d</", f"<NewGuestAccessEnabled>{value}</", template)
            return httpx.Response(200, text=body)

        return handler

    def _setter(self, band: str) -> Any:
        def handler(req: httpx.Request) -> httpx.Response:
            m = re.search(r"<NewGuestAccessEnabled>(\d)<", req.content.decode())
            on = bool(m and m.group(1) == "1")
            self.writes.append((band, on))
            if self.ignore:
                self.ignore -= 1
            elif self.lag:
                self._pending[band], self._lag_left = on, self.lag
            else:
                self.state[band] = on
            self.router.down = self.outage
            return httpx.Response(200, text=code_only("000"))

        return handler


NIGHT = datetime(2026, 9, 13, 2, 0, tzinfo=NY).astimezone(UTC)  # inside 21:00-07:00 in NY and UTC
DAY = datetime(2026, 9, 13, 13, 0, tzinfo=NY).astimezone(UTC)  # outside it in both


async def test_guest_schedule_turns_guest_off_at_night_and_on_in_the_morning(
    ctl: ControlService, router: FakeRouter
) -> None:
    wifi = GuestWifi(router, on=True)
    rule = ctl.add_guest_schedule(start="21:00", end="07:00", days="daily", name="guest-night")
    assert rule.kind == "guest" and rule.target == "both"

    changes = await ctl.apply_guest_schedules(now=NIGHT)
    assert sorted((b.value, on) for b, on in changes) == [("2.4GHz", False), ("5GHz", False)]
    assert wifi.state == {"2.4": False, "5": False}

    next_morning = datetime(2026, 9, 13, 12, 0, tzinfo=NY).astimezone(UTC)
    changes = await ctl.apply_guest_schedules(now=next_morning)
    assert all(on for _, on in changes) and len(changes) == 2
    assert wifi.state == {"2.4": True, "5": True}
    assert "guest-wifi" in ctl.changes_log.read_text()


async def test_guest_schedule_respects_a_manual_change_until_the_next_edge(
    ctl: ControlService, router: FakeRouter
) -> None:
    wifi = GuestWifi(router, on=True)
    ctl.add_guest_schedule(start="21:00", end="07:00")
    await ctl.apply_guest_schedules(now=NIGHT)  # scheduled off
    wifi.state["2.4"] = True  # a parent turns the guest network back on by hand
    writes_before = len(wifi.writes)
    later = NIGHT.replace(minute=30)
    assert await ctl.apply_guest_schedules(now=later) == []  # no edge, so no fight
    assert len(wifi.writes) == writes_before and wifi.state["2.4"] is True


async def test_guest_schedule_never_forces_guest_on_at_first_sight(
    ctl: ControlService, router: FakeRouter
) -> None:
    wifi = GuestWifi(router, on=False)  # guest is off on purpose
    ctl.add_guest_schedule(start="21:00", end="07:00")
    assert await ctl.apply_guest_schedules(now=DAY) == []  # outside the window: leave it off
    assert wifi.writes == []


async def test_guest_schedule_band_and_removal(ctl: ControlService, router: FakeRouter) -> None:
    wifi = GuestWifi(router, on=True)
    rule = ctl.add_guest_schedule(start="21:00", end="07:00", band="5")
    changes = await ctl.apply_guest_schedules(now=NIGHT)
    assert [(b.value, on) for b, on in changes] == [("5GHz", False)]
    assert wifi.state == {"2.4": True, "5": False}  # 2.4 untouched
    assert rule.id is not None and ctl.remove_schedule(rule.id)
    assert await ctl.apply_guest_schedules(now=NIGHT) == []
    assert ctl.registry.get_state("guest_schedule:5GHz") is None  # memory cleared with the rule
    with pytest.raises(ValueError):
        ctl.add_guest_schedule(start="21:00", end="07:00", band="6")


def config_sessions(router: FakeRouter) -> list[list[str]]:
    """The actions sent inside each ConfigurationStarted / ConfigurationFinished pair."""
    sessions: list[list[str]] = []
    current: list[str] = []
    for call in router.calls:
        action = call.split("#")[1]
        if action == "ConfigurationStarted":
            current = []
            sessions.append(current)
        elif action.startswith("Set"):
            current.append(action)
    return sessions


def guest_log(ctl: ControlService) -> list[str]:
    return [
        line.split(None, 1)[1] for line in ctl.changes_log.read_text().splitlines() if "guest-wifi" in line
    ]


async def test_guest_bands_switch_one_at_a_time_through_the_outage(
    ctl: ControlService, router: FakeRouter
) -> None:
    # Regression: both bands in one session lost the 5 GHz switch, because the router applies the
    # 2.4 GHz one at once and stops answering while its radios restart.
    wifi = GuestWifi(router, on=True, outage=4)
    echoed: list[str] = []
    switches = await ctl.set_guest_wifi([Band.GHZ_2_4, Band.GHZ_5], False, echo=echoed.append)
    assert wifi.state == {"2.4": False, "5": False}
    assert config_sessions(router) == [["SetGuestAccessEnabled"], ["Set5GGuestAccessEnabled"]]
    assert [(s.band.value, s.sent) for s in switches] == [("2.4GHz", 1), ("5GHz", 1)]
    log = "\n".join(guest_log(ctl))
    steps = ["2.4GHz  off requested", "2.4GHz  off confirmed", "5GHz  off requested", "5GHz  off confirmed"]
    assert [log.index(step) for step in steps] == sorted(log.index(step) for step in steps)
    assert any("waiting for the router" in e for e in echoed)


async def test_guest_band_already_in_state_is_not_switched(ctl: ControlService, router: FakeRouter) -> None:
    wifi = GuestWifi(router, on=True)
    wifi.state["5"] = False
    switches = await ctl.set_guest_wifi([Band.GHZ_2_4, Band.GHZ_5], True)
    assert wifi.writes == [("5", True)]  # 2.4 was on already: no send, no radio restart
    assert [s.sent for s in switches] == [0, 1]
    assert "already on" in guest_log(ctl)[0]


async def test_guest_switch_the_router_lost_is_sent_again(ctl: ControlService, router: FakeRouter) -> None:
    wifi = GuestWifi(router, on=True, outage=2)
    wifi.ignore = 1
    [switch] = await ctl.set_guest_wifi([Band.GHZ_5], False)
    assert switch.sent == 2 and wifi.state["5"] is False
    assert any("attempt 2" in line for line in guest_log(ctl))


async def test_guest_switch_that_shows_late_is_not_sent_again(
    ctl: ControlService, router: FakeRouter
) -> None:
    # The router acknowledges a switch before it shows it; resending then would restart the radios again.
    wifi = GuestWifi(router, on=True)
    wifi.lag = 3
    ctl.guest_settle_s = 60.0
    [switch] = await ctl.set_guest_wifi([Band.GHZ_5], False)
    assert switch.sent == 1 and wifi.writes == [("5", False)] and wifi.state["5"] is False


async def test_guest_switch_that_never_shows_raises(ctl: ControlService, router: FakeRouter) -> None:
    wifi = GuestWifi(router, on=True)
    wifi.ignore = 99
    with pytest.raises(GuestWifiUnconfirmed):
        await ctl.set_guest_wifi([Band.GHZ_5], False)
    assert len(wifi.writes) == ctl.guest_attempts
    assert "NOT confirmed" in guest_log(ctl)[-1]


async def test_guest_switch_gives_up_when_the_router_stays_down(
    ctl: ControlService, router: FakeRouter
) -> None:
    GuestWifi(router, on=True, outage=10_000)
    ctl.guest_confirm_timeout_s = 0.0
    with pytest.raises(GuestWifiUnconfirmed, match="stopped answering"):
        await ctl.set_guest_wifi([Band.GHZ_2_4, Band.GHZ_5], False)
    assert not any("5GHz" in line for line in guest_log(ctl))  # 5 GHz never sent into the outage


async def test_guest_schedule_retries_a_lost_switch_on_the_next_pass(
    ctl: ControlService, router: FakeRouter
) -> None:
    wifi = GuestWifi(router, on=True)
    ctl.add_guest_schedule(start="21:00", end="07:00")
    wifi.ignore = 99  # the router loses every attempt this pass
    with pytest.raises(GuestWifiUnconfirmed):
        await ctl.apply_guest_schedules(now=NIGHT)
    assert ctl.registry.get_state("guest_schedule:2.4GHz") is None  # not recorded, so it retries
    wifi.ignore = 0
    changes = await ctl.apply_guest_schedules(now=NIGHT)
    assert sorted((b.value, on) for b, on in changes) == [("2.4GHz", False), ("5GHz", False)]
    assert wifi.state == {"2.4": False, "5": False}
