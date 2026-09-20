"""FilterService: scope resolution, list management, and per-device/per-IP policy precedence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from curfew.dnsfilter import Mode
from curfew.models import Band, Device
from curfew.registry import Registry
from curfew.services.filtering import FilterService

T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
KID_MAC = "CE:46:A0:00:00:09"
DAD_MAC = "EA:82:62:00:00:03"


@pytest.fixture
def svc(tmp_path: Path) -> FilterService:
    reg = Registry(tmp_path / "registry.db")
    reg.record_scan(
        [
            Device(mac=KID_MAC, ip="192.168.1.93", name="iPad", band=Band.GHZ_2_4),
            Device(mac=DAD_MAC, ip="192.168.1.50", name="Laptop", band=Band.GHZ_5),
        ],
        now=T0,
    )
    reg.set_identity(KID_MAC, nickname="Misha iPad", owner="Michael", tags=["kids"])
    reg.set_identity(DAD_MAC, nickname="Dad laptop", owner="Sergey", tags=["parents"])
    service = FilterService(reg)
    yield service  # type: ignore[misc]
    reg.close()


def test_resolve_scope_prefers_group_then_owner_then_device(svc: FilterService) -> None:
    assert svc.resolve_scope("all")[:2] == ("global", "")
    assert svc.resolve_scope("kids")[:2] == ("group", "kids")
    assert svc.resolve_scope("Sergey")[:2] == ("owner", "Sergey")
    assert svc.resolve_scope("Misha iPad")[:2] == ("device", KID_MAC)
    with pytest.raises(LookupError):
        svc.resolve_scope("nobody")


def test_group_blacklist_applies_to_members(svc: FilterService) -> None:
    svc.set_mode("kids", "blacklist")
    svc.add("kids", "youtube.com", block=True)
    kid = svc.registry.get(KID_MAC)
    assert kid is not None
    policy = svc.policy_for_device(kid)
    assert policy.mode is Mode.BLACKLIST
    assert policy.block == {"youtube.com"}
    assert policy.scope == "group kids"
    # a device outside the group has no policy
    dad = svc.registry.get(DAD_MAC)
    assert dad is not None and svc.policy_for_device(dad).mode is Mode.OFF


def test_device_scope_overrides_group(svc: FilterService) -> None:
    svc.set_mode("kids", "blacklist")
    svc.add("kids", "youtube.com", block=True)
    # a stricter device-level whitelist wins over the group blacklist
    svc.set_mode("Misha iPad", "whitelist")
    svc.add("Misha iPad", "school.edu", block=False)
    kid = svc.registry.get(KID_MAC)
    assert kid is not None
    policy = svc.policy_for_device(kid)
    assert policy.mode is Mode.WHITELIST
    assert policy.allow == {"school.edu"}
    assert policy.scope.startswith("device")


def test_policy_for_ip_maps_client_to_device(svc: FilterService) -> None:
    svc.set_mode("kids", "blacklist")
    svc.add("kids", "tiktok.com", block=True)
    assert svc.policy_for_ip("192.168.1.93").block == {"tiktok.com"}  # the kid's iPad
    assert svc.policy_for_ip("192.168.1.50").mode is Mode.OFF  # dad's laptop, unfiltered
    assert svc.policy_for_ip("10.0.0.1").mode is Mode.OFF  # unknown client -> global (unset)


def test_off_mode_clears_the_scope(svc: FilterService) -> None:
    svc.set_mode("kids", "blacklist")
    assert svc.registry.get_filter_mode("group", "kids") == "blacklist"
    svc.set_mode("kids", "off")
    assert svc.registry.get_filter_mode("group", "kids") == "off"


def test_remove_and_list_rules(svc: FilterService) -> None:
    svc.add("kids", "youtube.com", block=True)
    svc.add("kids", "tiktok.com", block=True)
    assert len(svc.rules("kids")) == 2
    assert svc.remove("kids", "youtube.com", block=True) is True
    assert svc.remove("kids", "youtube.com", block=True) is False  # already gone
    remaining = svc.rules("kids")
    assert [r[3] for r in remaining] == ["tiktok.com"]


def test_invalid_mode_rejected(svc: FilterService) -> None:
    with pytest.raises(ValueError):
        svc.set_mode("kids", "sometimes")


def test_paused_device_resolves_nothing(svc: FilterService) -> None:
    svc.registry.set_pause(KID_MAC, "192.168.1.93")
    policy = svc.policy_for_ip("192.168.1.93")
    assert policy.mode is Mode.WHITELIST and policy.allow == set()  # empty whitelist blocks everything
    assert policy.scope == "paused"


def test_router_blocked_device_resolves_nothing(svc: FilterService) -> None:
    svc.registry.set_access_state(KID_MAC, allow=False)
    policy = svc.policy_for_ip("192.168.1.93")
    assert policy.mode is Mode.WHITELIST and policy.allow == set()
    assert policy.scope == "blocked"


def test_cut_state_overrides_filter_rules(svc: FilterService) -> None:
    # even with a permissive group whitelist, a paused device still resolves nothing
    svc.set_mode("kids", "whitelist")
    svc.add("kids", "school.edu", block=False)
    svc.registry.set_pause(KID_MAC, "192.168.1.93")
    assert svc.policy_for_ip("192.168.1.93").scope == "paused"


def test_uncut_device_resolves_normally(svc: FilterService) -> None:
    assert svc.policy_for_ip("192.168.1.50").mode is Mode.OFF  # dad's laptop, not cut, no filter
