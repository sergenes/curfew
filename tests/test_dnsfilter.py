"""DNS filter primitives: query parsing, block-reply building, matching, and the decision."""

from __future__ import annotations

import struct

import pytest

from curfew.dnsfilter import (
    RCODE_NXDOMAIN,
    DnsError,
    Mode,
    Policy,
    build_block_reply,
    domain_matches,
    is_blocked,
    parse_qname,
)


def query_for(name: str, *, tid: int = 0x1234, rd: bool = True) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
    header = struct.pack("!HHHHHH", tid, 0x0100 if rd else 0x0000, 1, 0, 0, 0)
    return header + labels + b"\x00" + struct.pack("!HH", 1, 1)  # QTYPE=A, QCLASS=IN


def test_parse_qname() -> None:
    assert parse_qname(query_for("www.youtube.com")) == "www.youtube.com"
    assert parse_qname(query_for("Example.COM")) == "example.com"  # lowercased
    with pytest.raises(DnsError):
        parse_qname(b"\x00\x00")  # too short


def test_build_block_reply_is_an_nxdomain_echo() -> None:
    q = query_for("youtube.com", tid=0xABCD)
    r = build_block_reply(q)
    assert r[0:2] == q[0:2]  # same transaction id
    assert r[2] & 0x80  # QR set: it is a response
    assert (r[3] & 0x0F) == RCODE_NXDOMAIN  # NXDOMAIN
    assert r[4:6] == q[4:6]  # question count preserved
    assert r[6:12] == b"\x00\x00\x00\x00\x00\x00"  # no answers/authority/additional
    assert r[12:] == q[12:]  # question echoed verbatim


def test_domain_matches_on_label_boundaries() -> None:
    block = {"youtube.com"}
    assert domain_matches("youtube.com", block)
    assert domain_matches("www.youtube.com", block)
    assert domain_matches("m.youtube.com", block)
    assert not domain_matches("notyoutube.com", block)
    assert not domain_matches("youtube.com.evil.com", block)


def test_is_blocked_across_modes() -> None:
    off = Policy(mode=Mode.OFF)
    assert not is_blocked("youtube.com", off)

    black = Policy(mode=Mode.BLACKLIST, block={"youtube.com", "tiktok.com"})
    assert is_blocked("www.youtube.com", black)
    assert not is_blocked("wikipedia.org", black)

    white = Policy(mode=Mode.WHITELIST, allow={"school.edu", "wikipedia.org"})
    assert not is_blocked("portal.school.edu", white)  # allowed
    assert is_blocked("youtube.com", white)  # everything else blocked
