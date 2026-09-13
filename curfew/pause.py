"""Eclipse Pause: cut one device's internet on the LAN by ARP interception.

This is a self-hosted, subscription-free replacement for Circle's "Pause". It works at
the same layer Circle does: we tell the target device that the router's IP is at *our*
MAC address, so the device sends its internet-bound traffic to us. We do not forward it,
so the device loses internet within seconds. It stays on wifi and can still reach other
LAN devices, exactly like Circle's pause.

Nothing here touches the router. A separate persistent layer (MAC Access Control) is
handled elsewhere; the two are independent.

Enforcement must be continuous: the real router keeps announcing the correct mapping, so
we re-send the spoof every couple of seconds. That loop lives in the `eclipse` daemon.
Sending raw ARP frames needs root.

The packet building and safety logic here are pure and unit tested. The actual send and
address resolution sit behind the `Sender` protocol so a different backend (a zero
dependency Linux AF_PACKET sender) can drop in later without touching the engine.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ETH_P_ARP = 0x0806
ARP_REPLY = 2
HEARTBEAT_FILE = "eclipse.heartbeat"


class PauseError(RuntimeError):
    pass


# -- pure packet helpers -----------------------------------------------------


def mac_to_bytes(mac: str) -> bytes:
    raw = mac.replace(":", "").replace("-", "")
    if len(raw) != 12:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes.fromhex(raw)


def ip_to_bytes(ip: str) -> bytes:
    parts = ip.split(".")
    if len(parts) != 4:
        raise ValueError(f"not an IPv4 address: {ip!r}")
    return bytes(int(p) for p in parts)


def build_arp_reply(
    *,
    src_mac: str,
    dst_mac: str,
    sender_mac: str,
    sender_ip: str,
    target_mac: str,
    target_ip: str,
) -> bytes:
    """An Ethernet + ARP reply frame claiming sender_ip is at sender_mac, addressed to the target."""
    eth = mac_to_bytes(dst_mac) + mac_to_bytes(src_mac) + struct.pack("!H", ETH_P_ARP)
    arp = (
        struct.pack("!HHBBH", 1, 0x0800, 6, 4, ARP_REPLY)
        + mac_to_bytes(sender_mac)
        + ip_to_bytes(sender_ip)
        + mac_to_bytes(target_mac)
        + ip_to_bytes(target_ip)
    )
    return eth + arp


def parse_arp_reply(frame: bytes) -> dict[str, str]:
    """Inverse of build_arp_reply, for tests and the doctor command."""
    if len(frame) < 42 or struct.unpack("!H", frame[12:14])[0] != ETH_P_ARP:
        raise ValueError("not an ARP frame")

    def mac(b: bytes) -> str:
        return ":".join(f"{x:02X}" for x in b)

    def ip(b: bytes) -> str:
        return ".".join(str(x) for x in b)

    return {
        "dst_mac": mac(frame[0:6]),
        "src_mac": mac(frame[6:12]),
        "sender_mac": mac(frame[22:28]),
        "sender_ip": ip(frame[28:32]),
        "target_mac": mac(frame[32:38]),
        "target_ip": ip(frame[38:42]),
    }


# -- network context and send backend ----------------------------------------


@dataclass(frozen=True)
class NetworkInfo:
    iface: str
    our_mac: str
    our_ip: str
    gateway_ip: str
    gateway_mac: str


class Sender(Protocol):
    def send(self, frame: bytes) -> None: ...
    def close(self) -> None: ...


class ScapySender:
    """Sends raw Ethernet frames via scapy. Works on macOS and Linux. Needs root."""

    def __init__(self, iface: str) -> None:
        try:
            from scapy.all import conf  # noqa: PLC0415
        except ImportError as err:  # pragma: no cover
            raise PauseError("scapy is required for Eclipse Pause: uv add scapy") from err
        self._iface = iface
        self._sock = conf.L2socket(iface=iface)

    def send(self, frame: bytes) -> None:
        from scapy.all import Ether  # type: ignore[attr-defined]  # noqa: PLC0415

        self._sock.send(Ether(frame))

    def close(self) -> None:
        self._sock.close()


def build_network_info(gateway_ip: str, iface: str | None = None) -> NetworkInfo:
    """Resolve our interface, our MAC/IP and the router's MAC. Raises PauseError on failure (needs root)."""
    try:
        from scapy.all import (  # type: ignore[attr-defined]  # noqa: PLC0415
            conf,
            get_if_addr,
            get_if_hwaddr,
            getmacbyip,
        )
    except ImportError as err:  # pragma: no cover
        raise PauseError("scapy is required for Eclipse Pause: uv add scapy") from err
    resolved_iface = str(iface or conf.iface)
    our_mac = str(get_if_hwaddr(resolved_iface))
    our_ip = str(get_if_addr(resolved_iface))
    gateway_mac = getmacbyip(gateway_ip)
    if not gateway_mac:
        raise PauseError(
            f"could not resolve the router MAC for {gateway_ip}. "
            "Run as root (sudo), and check the interface is on the LAN."
        )
    return NetworkInfo(
        iface=resolved_iface,
        our_mac=our_mac,
        our_ip=our_ip,
        gateway_ip=gateway_ip,
        gateway_mac=str(gateway_mac),
    )


# -- the engine --------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    mac: str
    ip: str


class PauseEngine:
    """Turns a set of paused targets into ARP frames. One send per call; the loop lives in the daemon."""

    def __init__(self, net: NetworkInfo, sender: Sender, *, bidirectional: bool = False) -> None:
        self.net = net
        self.sender = sender
        # When True, also poison the router so the victim's return traffic dies too, not just its
        # outbound. This holds a streaming device that would otherwise recover the real router MAC
        # between spoofs. It only touches the router's ARP entry for this one victim IP.
        self.bidirectional = bidirectional

    def assert_safe(self, target: Target) -> None:
        if not target.ip or not target.mac:
            raise PauseError("target needs both a MAC and a current IP")
        if target.ip == self.net.gateway_ip:
            raise PauseError("refusing to pause the router itself")
        if target.ip == self.net.our_ip:
            raise PauseError("refusing to pause this host")
        if target.mac.upper() in {self.net.our_mac.upper(), self.net.gateway_mac.upper()}:
            raise PauseError("refusing to pause the router or this host")

    def spoof(self, target: Target) -> None:
        """Redirect the target's traffic to us so it dies. Two-way also blackholes the return path."""
        self.assert_safe(target)
        # Tell the victim the router's IP is at our MAC, so its outbound traffic comes to us.
        self.sender.send(
            build_arp_reply(
                src_mac=self.net.our_mac,
                dst_mac=target.mac,
                sender_mac=self.net.our_mac,
                sender_ip=self.net.gateway_ip,
                target_mac=target.mac,
                target_ip=target.ip,
            )
        )
        if self.bidirectional:
            # Tell the router the victim's IP is at our MAC, so the victim's inbound traffic dies too.
            self.sender.send(
                build_arp_reply(
                    src_mac=self.net.our_mac,
                    dst_mac=self.net.gateway_mac,
                    sender_mac=self.net.our_mac,
                    sender_ip=target.ip,
                    target_mac=self.net.gateway_mac,
                    target_ip=self.net.gateway_ip,
                )
            )

    def heal(self, target: Target, *, repeat: int = 3) -> None:
        """Restore both ARP caches to the real mappings so the device recovers at once."""
        if not target.ip or not target.mac:
            return
        victim = build_arp_reply(
            src_mac=self.net.our_mac,
            dst_mac=target.mac,
            sender_mac=self.net.gateway_mac,
            sender_ip=self.net.gateway_ip,
            target_mac=target.mac,
            target_ip=target.ip,
        )
        router = build_arp_reply(
            src_mac=self.net.our_mac,
            dst_mac=self.net.gateway_mac,
            sender_mac=target.mac,
            sender_ip=target.ip,
            target_mac=self.net.gateway_mac,
            target_ip=self.net.gateway_ip,
        )
        for _ in range(repeat):
            self.sender.send(victim)
            if self.bidirectional:
                self.sender.send(router)

    def close(self) -> None:
        self.sender.close()


def diff_targets(prev: dict[str, Target], current: dict[str, Target]) -> tuple[list[Target], list[Target]]:
    """(added, removed) keyed by MAC, so the daemon knows which devices to newly spoof or to heal."""
    added = [t for mac, t in current.items() if mac not in prev]
    removed = [t for mac, t in prev.items() if mac not in current]
    return added, removed


# -- heartbeat: lets other processes tell whether the daemon is enforcing -----


def write_heartbeat(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / HEARTBEAT_FILE).write_text(str(time.time()))


def eclipse_running(data_dir: Path, *, max_age_s: float = 15.0) -> bool:
    path = data_dir / HEARTBEAT_FILE
    try:
        return (time.time() - path.stat().st_mtime) < max_age_s
    except OSError:
        return False
