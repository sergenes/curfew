"""Eclipse Pause: ARP frame building, engine safety, spoof/heal, target diffing, heartbeat."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from curfew.pause import (
    NetworkInfo,
    PauseEngine,
    PauseError,
    Target,
    build_arp_reply,
    diff_targets,
    eclipse_running,
    ip_to_bytes,
    mac_to_bytes,
    parse_arp_reply,
    write_heartbeat,
)

NET = NetworkInfo(
    iface="en0",
    our_mac="AA:AA:AA:AA:AA:AA",
    our_ip="192.168.1.50",
    gateway_ip="192.168.1.1",
    gateway_mac="94:18:65:00:00:0C",
)
KID = Target(mac="CE:46:A0:00:00:09", ip="192.168.1.93")


class FakeSender:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def send(self, frame: bytes) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


def test_mac_and_ip_encoding() -> None:
    assert mac_to_bytes("CE:46:A0:00:00:09") == bytes([0xCE, 0x46, 0xA0, 0x00, 0x00, 0x09])
    assert mac_to_bytes("ce-46-a0-00-00-09") == bytes([0xCE, 0x46, 0xA0, 0x00, 0x00, 0x09])
    assert ip_to_bytes("192.168.1.1") == bytes([192, 168, 1, 1])
    with pytest.raises(ValueError):
        mac_to_bytes("nope")
    with pytest.raises(ValueError):
        ip_to_bytes("1.2.3")


def test_arp_reply_roundtrips() -> None:
    frame = build_arp_reply(
        src_mac=NET.our_mac,
        dst_mac=KID.mac,
        sender_mac=NET.our_mac,
        sender_ip=NET.gateway_ip,
        target_mac=KID.mac,
        target_ip=KID.ip,
    )
    assert len(frame) == 42
    parsed = parse_arp_reply(frame)
    assert parsed["dst_mac"] == KID.mac
    assert parsed["src_mac"] == NET.our_mac
    assert parsed["sender_ip"] == NET.gateway_ip
    assert parsed["sender_mac"] == NET.our_mac  # the lie: gateway IP claimed at our MAC
    assert parsed["target_ip"] == KID.ip


def test_spoof_claims_gateway_is_at_our_mac() -> None:
    sender = FakeSender()
    engine = PauseEngine(NET, sender)
    engine.spoof(KID)
    p = parse_arp_reply(sender.frames[0])
    assert p["dst_mac"] == KID.mac  # sent to the victim
    assert p["sender_ip"] == NET.gateway_ip  # about the router
    assert p["sender_mac"] == NET.our_mac  # but pointing at us -> traffic black-holes


def test_heal_restores_real_gateway_mac() -> None:
    sender = FakeSender()
    engine = PauseEngine(NET, sender)
    engine.heal(KID, repeat=3)
    assert len(sender.frames) == 3
    p = parse_arp_reply(sender.frames[0])
    assert p["sender_ip"] == NET.gateway_ip
    assert p["sender_mac"] == NET.gateway_mac  # the truth restored


def test_bidirectional_spoof_poisons_both_the_victim_and_the_router() -> None:
    sender = FakeSender()
    engine = PauseEngine(NET, sender, bidirectional=True)
    engine.spoof(KID)
    assert len(sender.frames) == 2
    victim = parse_arp_reply(sender.frames[0])
    router = parse_arp_reply(sender.frames[1])
    # to the victim: the router's IP is at our MAC (its outbound dies)
    assert victim["dst_mac"] == KID.mac
    assert victim["sender_ip"] == NET.gateway_ip and victim["sender_mac"] == NET.our_mac
    # to the router: the victim's IP is at our MAC (the return path dies)
    assert router["dst_mac"] == NET.gateway_mac
    assert router["sender_ip"] == KID.ip and router["sender_mac"] == NET.our_mac


def test_bidirectional_heal_restores_both_sides() -> None:
    sender = FakeSender()
    engine = PauseEngine(NET, sender, bidirectional=True)
    engine.heal(KID, repeat=2)
    assert len(sender.frames) == 4  # victim + router, twice
    router = parse_arp_reply(sender.frames[1])
    assert router["dst_mac"] == NET.gateway_mac
    assert router["sender_ip"] == KID.ip and router["sender_mac"] == KID.mac  # truth restored to the router


def test_one_way_is_the_engine_default() -> None:
    sender = FakeSender()
    PauseEngine(NET, sender).spoof(KID)
    assert len(sender.frames) == 1  # victim only, unless bidirectional is requested


def test_safety_refuses_router_and_self() -> None:
    engine = PauseEngine(NET, FakeSender())
    with pytest.raises(PauseError, match="router itself"):
        engine.spoof(Target(mac="11:22:33:44:55:66", ip=NET.gateway_ip))
    with pytest.raises(PauseError, match="this host"):
        engine.spoof(Target(mac="11:22:33:44:55:66", ip=NET.our_ip))
    with pytest.raises(PauseError, match="router or this host"):
        engine.spoof(Target(mac=NET.gateway_mac, ip="192.168.1.200"))
    with pytest.raises(PauseError, match="MAC and a current IP"):
        engine.spoof(Target(mac="11:22:33:44:55:66", ip=""))


def test_heal_is_a_noop_without_address() -> None:
    sender = FakeSender()
    PauseEngine(NET, sender).heal(Target(mac="", ip=""))
    assert sender.frames == []


def test_diff_targets() -> None:
    a = Target(mac="A", ip="1")
    b = Target(mac="B", ip="2")
    c = Target(mac="C", ip="3")
    added, removed = diff_targets({"A": a, "B": b}, {"B": b, "C": c})
    assert [t.mac for t in added] == ["C"]
    assert [t.mac for t in removed] == ["A"]


def test_heartbeat(tmp_path: Path) -> None:
    assert eclipse_running(tmp_path) is False
    write_heartbeat(tmp_path)
    assert eclipse_running(tmp_path) is True
    assert eclipse_running(tmp_path, max_age_s=-1) is False  # anything is "too old"
    _ = time  # keep import used across edits
