"""Registry: scans, presence sessions, identity, resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from curfew.models import Band, Device
from curfew.registry import Registry, parse_since

T0 = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def dev(mac: str, ip: str = "192.168.1.10", name: str = "Unknown", band: Band = Band.GHZ_2_4) -> Device:
    return Device(mac=mac, ip=ip, name=name, band=band)


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    r = Registry(tmp_path / "registry.db", offline_after=2)
    yield r  # type: ignore[misc]
    r.close()


def test_first_scan_reports_everything_as_new(reg: Registry) -> None:
    delta = reg.record_scan([dev("aa:bb:cc:00:00:01"), dev("aa:bb:cc:00:00:02", ip="192.168.1.11")], now=T0)
    assert delta.present == 2
    assert {k.mac for k in delta.new} == {"AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"}
    assert delta.returned == [] and delta.left == []
    k = reg.get("AA:BB:CC:00:00:01")
    assert k is not None and k.online and k.first_seen == T0


def test_missing_once_is_not_leaving(reg: Registry) -> None:
    reg.record_scan([dev("aa:bb:cc:00:00:01")], now=T0)
    d1 = reg.record_scan([], now=T0 + timedelta(minutes=1))
    assert d1.left == []
    assert reg.get("AA:BB:CC:00:00:01").online  # type: ignore[union-attr]
    d2 = reg.record_scan([], now=T0 + timedelta(minutes=2))
    assert [k.mac for k in d2.left] == ["AA:BB:CC:00:00:01"]
    assert not reg.get("AA:BB:CC:00:00:01").online  # type: ignore[union-attr]


def test_returning_device_opens_new_session(reg: Registry) -> None:
    mac = "aa:bb:cc:00:00:01"
    reg.record_scan([dev(mac)], now=T0)
    reg.record_scan([dev(mac)], now=T0 + timedelta(minutes=1))
    reg.record_scan([], now=T0 + timedelta(minutes=2))
    reg.record_scan([], now=T0 + timedelta(minutes=3))  # closes
    delta = reg.record_scan([dev(mac, ip="192.168.1.99")], now=T0 + timedelta(hours=2))
    assert [k.mac for k in delta.returned] == ["AA:BB:CC:00:00:01"]
    sessions = reg.sessions(mac)
    assert len(sessions) == 2
    assert sessions[0].ended_at is None and sessions[0].ip == "192.168.1.99"
    assert sessions[1].ended_at == T0 + timedelta(minutes=1)
    assert sessions[1].duration == timedelta(minutes=1)


def test_vendor_lookup_applied_once(reg: Registry) -> None:
    calls: list[str] = []

    def lookup(mac: str) -> str:
        calls.append(mac)
        return "ACME"

    reg.record_scan([dev("aa:bb:cc:00:00:01")], now=T0, vendor_lookup=lookup)
    reg.record_scan([dev("aa:bb:cc:00:00:01")], now=T0 + timedelta(minutes=1), vendor_lookup=lookup)
    assert calls == ["AA:BB:CC:00:00:01"]
    assert reg.get("aa:bb:cc:00:00:01").vendor == "ACME"  # type: ignore[union-attr]


def test_identity_and_resolution(reg: Registry) -> None:
    reg.record_scan(
        [dev("aa:bb:cc:00:00:01", ip="192.168.1.5", name="iPhone"), dev("aa:bb:cc:00:00:02", name="iPhone")],
        now=T0,
    )
    k = reg.set_identity("aa-bb-cc-00-00-01", nickname="Sam's phone", owner="Sam", tags=["Kid", "phone", " "])
    assert k.nickname == "Sam's phone" and k.owner == "Sam" and k.tags == ["kid", "phone"]
    assert k.display_name == "Sam's phone"
    assert [c.mac for c in reg.resolve("sam's PHONE")] == ["AA:BB:CC:00:00:01"]
    assert [c.mac for c in reg.resolve("192.168.1.5")] == ["AA:BB:CC:00:00:01"]
    assert len(reg.resolve("iphone")) == 2  # router name shared by two devices
    assert reg.resolve("nobody") == []
    assert [c.mac for c in reg.by_owner("sam")] == ["AA:BB:CC:00:00:01"]
    assert [c.mac for c in reg.by_tag("KID")] == ["AA:BB:CC:00:00:01"]
    # clearing with "" and leaving alone with None
    k = reg.set_identity("aa:bb:cc:00:00:01", owner="", tags=None)
    assert k.owner == "" and k.tags == ["kid", "phone"]


def test_identity_for_unseen_mac_fails(reg: Registry) -> None:
    with pytest.raises(KeyError):
        reg.set_identity("00:00:00:00:00:00", nickname="ghost")


def test_merge_folds_old_record_into_new(reg: Registry) -> None:
    old = "ce:46:a0:00:00:01"  # randomized wifi MAC, carries the identity
    new = "94:18:65:00:00:02"  # real MAC after Private Wi-Fi Address is turned off
    reg.record_scan([dev(old, ip="192.168.1.93")], now=T0)
    reg.set_identity(old, nickname="Misha's iPad", owner="Michael", tags=["kids"], notes="bedtime 9pm")
    reg.record_scan([dev(new, ip="192.168.1.77")], now=T0 + timedelta(days=1))

    merged = reg.merge(old, new)

    assert merged.mac == "94:18:65:00:00:02"
    assert merged.nickname == "Misha's iPad"
    assert merged.owner == "Michael"
    assert merged.tags == ["kids"]
    assert merged.notes == "bedtime 9pm"
    assert merged.first_seen == T0  # the earlier sighting is kept
    assert merged.last_ip == "192.168.1.77"  # the survivor's own live fields stay
    assert reg.get(old) is None
    # presence history moved across, so resolving the new MAC finds both sessions
    assert len(reg.sessions(new)) == 2


def test_merge_unions_tags_and_keeps_survivor_identity(reg: Registry) -> None:
    old = "ce:46:a0:00:00:01"
    new = "94:18:65:00:00:02"
    reg.record_scan([dev(old), dev(new)], now=T0)
    reg.set_identity(old, owner="Michael", tags=["kids", "tablet"])
    reg.set_identity(new, nickname="iPad Pro", tags=["loaner"])

    merged = reg.merge(old, new)

    assert merged.nickname == "iPad Pro"  # survivor's own value wins over the old (empty) one
    assert merged.owner == "Michael"  # survivor was empty, inherits from old
    assert merged.tags == ["kids", "loaner", "tablet"]  # union, sorted


def test_merge_moves_a_block_only_if_survivor_has_none(reg: Registry) -> None:
    old = "ce:46:a0:00:00:01"
    new = "94:18:65:00:00:02"
    reg.record_scan([dev(old), dev(new, ip="192.168.1.77")], now=T0)
    reg.set_override(old, allow=False, until=None, reason="bedtime")

    merged = reg.merge(old, new)

    overrides = reg.overrides()
    assert old not in overrides
    assert overrides[merged.mac].allow is False and overrides[merged.mac].reason == "bedtime"


def test_merge_rejects_same_device(reg: Registry) -> None:
    reg.record_scan([dev("ce:46:a0:00:00:01")], now=T0)
    with pytest.raises(ValueError):
        reg.merge("ce:46:a0:00:00:01", "CE-46-A0-00-00-01")


def test_merge_requires_both_to_exist(reg: Registry) -> None:
    reg.record_scan([dev("ce:46:a0:00:00:01")], now=T0)
    with pytest.raises(KeyError):
        reg.merge("ce:46:a0:00:00:01", "94:18:65:00:00:02")


def test_forget_clears_block_and_pause(reg: Registry) -> None:
    mac = "ce:46:a0:00:00:01"
    reg.record_scan([dev(mac, ip="192.168.1.93")], now=T0)
    reg.set_override(mac, allow=False, until=None)
    reg.set_pause(mac, "192.168.1.93")
    assert reg.forget(mac) is True  # no foreign-key error from leftover child rows
    assert reg.get(mac) is None
    assert reg.overrides() == {} and reg.pauses() == []


def test_new_since_and_last_scan(reg: Registry) -> None:
    reg.record_scan([dev("aa:bb:cc:00:00:01")], now=T0)
    reg.record_scan([dev("aa:bb:cc:00:00:01"), dev("aa:bb:cc:00:00:02")], now=T0 + timedelta(days=3))
    fresh = reg.new_since(T0 + timedelta(days=1))
    assert [k.mac for k in fresh] == ["AA:BB:CC:00:00:02"]
    assert reg.last_scan() == T0 + timedelta(days=3)


def test_display_name_fallbacks(reg: Registry) -> None:
    reg.record_scan([Device(mac="aa:bb:cc:00:00:03", ip="", name="Unknown", model="Nest Cam")], now=T0)
    reg.record_scan(
        [Device(mac="4a:bb:cc:00:00:04", ip="", name="Unknown")], now=T0, vendor_lookup=lambda m: ""
    )
    assert reg.get("aa:bb:cc:00:00:03").display_name == "Nest Cam"  # type: ignore[union-attr]
    k = reg.get("4a:bb:cc:00:00:04")
    assert k is not None and k.display_name == "unknown" and k.randomized_mac is True


def test_parse_since() -> None:
    assert parse_since("7d", now=T0) == T0 - timedelta(days=7)
    assert parse_since("12H", now=T0) == T0 - timedelta(hours=12)
    assert parse_since("2w", now=T0) == T0 - timedelta(weeks=2)
    assert parse_since("2026-09-01", now=T0) == datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_since("yesterday", now=T0)
