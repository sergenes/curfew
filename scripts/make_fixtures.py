"""Build sanitized test fixtures from the raw discovery dumps.

Usage: uv run python scripts/make_fixtures.py

Copies selected discovery/*.xml files to tests/fixtures/, redacting secrets
(wifi passphrases, serial number, external IP, DNS), replacing MACs, SSIDs and host
names with synthetic values, and trimming long lists so the fixtures stay small and
safe to commit. Nothing from the real network is hard coded here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "discovery"
DST = ROOT / "tests" / "fixtures"

KEEP = [
    "DeviceInfo_GetInfo",
    "DeviceInfo_GetSysUpTime",
    "DeviceInfo_GetSystemInfo",
    "DeviceInfo_GetAttachDevice2",
    "DeviceInfo_GetSystemLogs",
    "DeviceConfig_GetBlockDeviceEnableStatus",
    "DeviceConfig_GetTrafficMeterEnabled",
    "DeviceConfig_GetTrafficMeterStatistics",
    "DeviceConfig_CheckNewFirmware",
    "LANConfigSecurity_GetInfo",
    "WANIPConnection_GetInfo",
    "WANEthernetLinkConfig_GetEthernetLinkStatus",
    "WLANConfiguration_GetInfo",
    "WLANConfiguration_Get5GInfo",
    "WLANConfiguration_GetGuestAccessEnabled",
    "WLANConfiguration_Get5GGuestAccessEnabled",
    "WLANConfiguration_GetGuestAccessNetworkInfo",
    "WLANConfiguration_Get5GGuestAccessNetworkInfo",
    "AdvancedQoS_GetQoSEnableStatus",  # a 501 example
]

REDACTIONS = [
    (re.compile(r"(<NewWPAPassphrase>)[^<]*"), r"\1redacted-passphrase"),
    (re.compile(r"(<NewKey>)[^<]*"), r"\1redacted-key"),
    (re.compile(r"(<SerialNumber>)[^<]*"), r"\1SERIAL0000001"),
    (re.compile(r"(<NewExternalIPAddress>)[^<]*"), r"\g<1>203.0.113.10"),
    (re.compile(r"(<NewDefaultGateway>)[^<]*"), r"\g<1>203.0.113.1"),
    (re.compile(r"(<NewDNSServers>)[^<]*"), r"\g<1>198.51.100.45 198.51.100.46"),
    # Host names like "Owners-iPhone" -> "iPhone" in the router log.
    (re.compile(r"(Device )\w+s-(\S+ with MAC)"), r"\1\2"),
]

MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b|\b[0-9A-Fa-f]{12}\b")
SSID_RE = re.compile(r"(<(?:New)?SSID>)([^<]*)(</)")


class Sanitizer:
    """Replace real MACs and SSIDs with synthetic ones, consistently across all fixtures.

    MACs keep their OUI (first three bytes) so vendor lookup and the randomized-MAC bit behave
    as on the real network; the device part becomes a sequence number in order of first sight.
    The router's own SSIDs become Home-2.4 and Home-5; NETGEAR default guest SSIDs are kept.
    """

    def __init__(self) -> None:
        self.macs: dict[str, str] = {}
        self.ssids: dict[str, str] = {}

    def seed_ssids(self, ssid_24: str, ssid_5: str) -> None:
        for text, fake in ((ssid_24, "Home-2.4"), (ssid_5, "Home-5")):
            m = SSID_RE.search(text)
            if m and not m.group(2).startswith("NETGEAR"):
                self.ssids[m.group(2)] = fake

    def mac(self, m: re.Match[str]) -> str:
        raw = m.group(0)
        key = raw.replace(":", "").upper()
        if key not in self.macs:
            self.macs[key] = f"{key[:6]}{len(self.macs) + 1:06X}"
        fake = self.macs[key]
        if ":" not in raw:
            return fake
        out = ":".join(fake[i : i + 2] for i in range(0, 12, 2))
        return out.lower() if raw.islower() else out

    def ssid(self, m: re.Match[str]) -> str:
        return m.group(1) + self.ssids.get(m.group(2), m.group(2)) + m.group(3)

    def apply(self, text: str) -> str:
        for pattern, repl in REDACTIONS:
            text = pattern.sub(repl, text)
        text = MAC_RE.sub(self.mac, text)
        return SSID_RE.sub(self.ssid, text)


def trim_devices(text: str, keep: int = 3) -> str:
    devices = re.findall(r"<Device>.*?</Device>\n?", text, flags=re.S)
    if len(devices) <= keep:
        return text
    head, _, rest = text.partition(devices[0])
    tail = rest.split(devices[-1], 1)[1]
    return head + "".join(devices[:keep]) + tail


def trim_logs(text: str, keep: int = 8) -> str:
    m = re.search(r"<NewLogDetails>(.*?)</NewLogDetails>", text, flags=re.S)
    if not m:
        return text
    lines = [line for line in m.group(1).splitlines() if line.strip()]
    return text[: m.start(1)] + "\n".join(lines[:keep]) + "\n" + text[m.end(1) :]


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    sanitizer = Sanitizer()
    sanitizer.seed_ssids(
        (SRC / "WLANConfiguration_GetInfo.xml").read_text(),
        (SRC / "WLANConfiguration_Get5GInfo.xml").read_text(),
    )
    for name in KEEP:
        text = (SRC / f"{name}.xml").read_text()
        if name.endswith("GetAttachDevice2"):
            text = trim_devices(text)
        if name.endswith("GetSystemLogs"):
            text = trim_logs(text)
        text = sanitizer.apply(text)
        (DST / f"{name}.xml").write_text(text)
        print(f"wrote {DST / name}.xml ({len(text)} bytes)")


if __name__ == "__main__":
    main()
