"""DNS filtering primitives: parse a query name, build a block reply, and decide allow vs block.

Everything here is pure and unit tested. The socket, the upstream forwarder and the per-client
policy lookup live in the daemon (curfew/services/dns.py), so this module never touches the network.

Two modes, evaluated per requesting device:
  - blacklist: block a name that matches the block list, forward everything else.
  - whitelist: forward a name that matches the allow list, block everything else.
  - off: forward everything.

Name matching is by domain suffix on label boundaries, so "youtube.com" also covers
"www.youtube.com" and "m.youtube.com", but never "notyoutube.com".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import StrEnum

RCODE_NXDOMAIN = 3
RCODE_REFUSED = 5


class Mode(StrEnum):
    OFF = "off"
    BLACKLIST = "blacklist"
    WHITELIST = "whitelist"


class DnsError(ValueError):
    pass


# -- wire format -------------------------------------------------------------


def _name_end(data: bytes, offset: int) -> int:
    """Offset just past the QNAME starting at `offset` (questions never use compression)."""
    i = offset
    while True:
        if i >= len(data):
            raise DnsError("truncated name")
        length = data[i]
        if length == 0:
            return i + 1
        if length & 0xC0:  # a compression pointer has no place in a question
            raise DnsError("unexpected compression in question")
        i += 1 + length


def parse_qname(data: bytes) -> str:
    """The first question's name, lowercased, without the trailing dot ("" for the root)."""
    if len(data) < 12:
        raise DnsError("short DNS message")
    labels: list[str] = []
    i = 12
    while True:
        if i >= len(data):
            raise DnsError("truncated name")
        length = data[i]
        if length == 0:
            break
        if length & 0xC0:
            raise DnsError("unexpected compression in question")
        labels.append(data[i + 1 : i + 1 + length].decode("latin-1").lower())
        i += 1 + length
    return ".".join(labels)


def question_end(data: bytes) -> int:
    """Offset just past the first question (name + qtype + qclass)."""
    return _name_end(data, 12) + 4


def build_block_reply(query: bytes, *, rcode: int = RCODE_NXDOMAIN) -> bytes:
    """A response to `query` carrying `rcode` and no answers, echoing the question."""
    if len(query) < 12:
        raise DnsError("short DNS message")
    tid = query[0:2]
    qflags0 = query[2]
    rd = qflags0 & 0x01
    opcode = (qflags0 >> 3) & 0x0F
    flags0 = 0x80 | (opcode << 3) | rd  # QR=1, keep opcode and RD
    flags1 = 0x80 | (rcode & 0x0F)  # RA=1, RCODE
    header = tid + bytes([flags0, flags1]) + query[4:6] + struct.pack("!HHH", 0, 0, 0)
    return header + query[12 : question_end(query)]


# -- matching and decision ---------------------------------------------------


def normalize_domain(pattern: str) -> str:
    return pattern.strip().lower().rstrip(".").removeprefix("*.")


def domain_matches(qname: str, patterns: set[str]) -> bool:
    """True if qname equals a pattern or is a subdomain of one, on label boundaries."""
    q = qname.strip().lower().rstrip(".")
    for raw in patterns:
        p = normalize_domain(raw)
        if p and (q == p or q.endswith("." + p)):
            return True
    return False


@dataclass(frozen=True)
class Policy:
    """The effective filtering policy for one device, resolved from its scope."""

    mode: Mode = Mode.OFF
    block: set[str] = field(default_factory=set)
    allow: set[str] = field(default_factory=set)
    scope: str = "none"  # human label of where the policy came from, for logging


def is_blocked(qname: str, policy: Policy) -> bool:
    """Whether a lookup of qname should be refused for a device on this policy."""
    if policy.mode is Mode.OFF:
        return False
    if policy.mode is Mode.BLACKLIST:
        return domain_matches(qname, policy.block)
    return not domain_matches(qname, policy.allow)  # whitelist: block unless allowed
