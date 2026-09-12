"""Phase 0 discovery: probe every known read-only SOAP action on the router.

Usage: uv run python scripts/discover.py

Reads credentials from .env, logs in with the SOAP v2 method, calls each action,
saves the raw XML response to discovery/<Service>_<Action>.xml and writes
discovery/SUMMARY.md with a capability matrix.
Values of secret-looking fields (passphrases, keys, passwords) are redacted in the summary,
and identifying values (serial, SSIDs, MACs, public IPs, host names) are partially masked with ***.
"""

from __future__ import annotations

import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "discovery"

SERVICE_PREFIX = "urn:NETGEAR-ROUTER:service:"
SESSION_ID = "A7D88AE69687E58D9A00"
SECRET_RE = re.compile(r"(passphrase|password|key|psk)", re.IGNORECASE)

# (service, action). Read-only calls only; nothing here changes router state.
ACTIONS: list[tuple[str, str]] = [
    # DeviceInfo
    ("DeviceInfo:1", "GetInfo"),
    ("DeviceInfo:1", "GetSysUpTime"),
    ("DeviceInfo:1", "GetSystemInfo"),
    ("DeviceInfo:1", "GetSupportFeatureListXML"),
    ("DeviceInfo:1", "GetAttachDevice"),
    ("DeviceInfo:1", "GetAttachDevice2"),
    ("DeviceInfo:1", "GetDeviceListAll"),
    ("DeviceInfo:1", "GetAllSatellites"),
    ("DeviceInfo:1", "GetSystemLogs"),
    # DeviceConfig
    ("DeviceConfig:1", "GetInfo"),
    ("DeviceConfig:1", "GetTimeZoneInfo"),
    ("DeviceConfig:1", "GetTrafficMeterEnabled"),
    ("DeviceConfig:1", "GetTrafficMeterOptions"),
    ("DeviceConfig:1", "GetTrafficMeterStatistics"),
    ("DeviceConfig:1", "GetBlockDeviceEnableStatus"),
    ("DeviceConfig:1", "GetBlockSiteInfo"),
    ("DeviceConfig:1", "GetQoSEnableStatus"),
    ("DeviceConfig:1", "CheckNewFirmware"),
    ("DeviceConfig:1", "GetDeviceConfig"),
    # LAN / WAN
    ("LANConfigSecurity:1", "GetInfo"),
    ("WANIPConnection:1", "GetInfo"),
    ("WANIPConnection:1", "GetConnectionTypeInfo"),
    ("WANIPConnection:1", "GetPortMappingInfo"),
    ("WANEthernetLinkConfig:1", "GetEthernetLinkStatus"),
    # ParentalControl
    ("ParentalControl:1", "GetEnableStatus"),
    ("ParentalControl:1", "GetAllMACAddresses"),
    ("ParentalControl:1", "GetDNSMasqDeviceID"),
    # AdvancedQoS
    ("AdvancedQoS:1", "GetQoSEnableStatus"),
    ("AdvancedQoS:1", "GetBandwidthControlOptions"),
    ("AdvancedQoS:1", "GetOOKLASpeedTestResult"),
    ("AdvancedQoS:1", "GetCurrentDeviceBandwidth"),
    ("AdvancedQoS:1", "GetCurrentAppBandwidth"),
    # WLANConfiguration
    ("WLANConfiguration:1", "GetInfo"),
    ("WLANConfiguration:1", "Get5GInfo"),
    ("WLANConfiguration:1", "GetChannelInfo"),
    ("WLANConfiguration:1", "Get5GChannelInfo"),
    ("WLANConfiguration:1", "GetAvailableChannel"),
    ("WLANConfiguration:1", "GetRegion"),
    ("WLANConfiguration:1", "GetWPASecurityKeys"),
    ("WLANConfiguration:1", "Get5GWPASecurityKeys"),
    ("WLANConfiguration:1", "GetGuestAccessEnabled"),
    ("WLANConfiguration:1", "GetGuestAccessEnabled2"),
    ("WLANConfiguration:1", "Get5GGuestAccessEnabled"),
    ("WLANConfiguration:1", "Get5G1GuestAccessEnabled"),
    ("WLANConfiguration:1", "Get5GGuestAccessEnabled2"),
    ("WLANConfiguration:1", "GetGuestAccessNetworkInfo"),
    ("WLANConfiguration:1", "Get5GGuestAccessNetworkInfo"),
    ("WLANConfiguration:1", "IsSmartConnectEnabled"),
    ("WLANConfiguration:1", "GetWLANMACAddress"),
]

ENVELOPE = """<?xml version="1.0" encoding="utf-8" standalone="no"?>
<SOAP-ENV:Envelope xmlns:SOAPSDK1="http://www.w3.org/2001/XMLSchema"
  xmlns:SOAPSDK2="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:SOAPSDK3="http://schemas.xmlsoap.org/soap/encoding/"
  xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
<SOAP-ENV:Header>
<SessionID>{session_id}</SessionID>
</SOAP-ENV:Header>
<SOAP-ENV:Body>
<M1:{action} xmlns:M1="{service}">
{params}</M1:{action}>
</SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""


@dataclass
class Result:
    service: str
    action: str
    http_status: int
    response_code: str
    elapsed_ms: int
    fields: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.response_code in {"0", "000", "0000"}


class Router:
    def __init__(self, host: str, port: int, ssl: bool, user: str, password: str):
        scheme = "https" if ssl else "http"
        self.url = f"{scheme}://{host}:{port}/soap/server_sa/"
        self.user = user
        self.password = password
        self.client = httpx.Client(verify=False, timeout=30)
        self.cookie: str | None = None

    def _headers(self, service: str, action: str) -> dict[str, str]:
        headers = {
            "SOAPAction": f"{SERVICE_PREFIX}{service}#{action}",
            "Cache-Control": "no-cache",
            "User-Agent": "curfew-discover",
            "Content-Type": "multipart/form-data",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def call(self, service: str, action: str, params: dict[str, str] | None = None) -> httpx.Response:
        body = "".join(f"<{k}>{v}</{k}>\n" for k, v in (params or {}).items())
        message = ENVELOPE.format(
            session_id=SESSION_ID, service=SERVICE_PREFIX + service, action=action, params=body
        )
        return self.client.post(self.url, headers=self._headers(service, action), content=message)

    def login(self) -> bool:
        resp = self.call("DeviceConfig:1", "SOAPLogin", {"Username": self.user, "Password": self.password})
        code = response_code(resp.text)
        if code not in {"0", "000", "0000"}:
            print(f"login failed: http {resp.status_code}, ResponseCode {code}")
            print(resp.text[:500])
            return False
        cookie = resp.headers.get("set-cookie")
        if not cookie:
            print("login ok but no Set-Cookie header, cannot continue")
            return False
        self.cookie = cookie.split(";")[0]
        return True


def response_code(text: str) -> str:
    m = re.search(r"<ResponseCode>(\w+)</ResponseCode>", text)
    return m.group(1) if m else "missing"


def parse_fields(text: str, action: str) -> dict[str, str]:
    """Flatten the <ActionResponse> node into tag -> text (first level only)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {"_parse_error": "response is not well formed XML"}
    node = next((el for el in root.iter() if local_name(el.tag) == f"{action}Response"), None)
    if node is None:
        return {}
    out: dict[str, str] = {}
    for child in node:
        if len(child):  # nested structure, summarize
            out[local_name(child.tag)] = f"<{len(child)} child nodes>"
        else:
            out[local_name(child.tag)] = (child.text or "").strip()
    return out


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


MAC_COLON_RE = re.compile(r"\b((?:[0-9A-Fa-f]{2}:){2}[0-9A-Fa-f]{2}):(?:[0-9A-Fa-f]{2}:){2}[0-9A-Fa-f]{2}\b")
MAC_PLAIN_RE = re.compile(r"\b([0-9A-F]{6})[0-9A-F]{6}\b")
PUBLIC_IP_RE = re.compile(r"\b(?!192\.168\.|10\.|127\.|0\.)(\d{1,3}\.\d{1,3})\.\d{1,3}\.\d{1,3}\b")
HOST_IN_LIST_RE = re.compile(r"(;)[^;]+(;(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2};)")


def mask(name: str, value: str) -> str:
    """Partially mask identifying values so the summary can be published."""
    if "serial" in name.lower():
        return value[:3] + "***" if value else value
    if "ssid" in name.lower() and "broadcast" not in name.lower():
        return "***"
    value = MAC_COLON_RE.sub(r"\1:**:**:**", value)
    value = MAC_PLAIN_RE.sub(r"\1******", value)
    value = PUBLIC_IP_RE.sub(r"\1.***.***", value)
    value = HOST_IN_LIST_RE.sub(r"\1***\2", value)
    return value


def redact(name: str, value: str) -> str:
    if SECRET_RE.search(name) and value:
        return "<redacted>"
    value = mask(name, value)
    if len(value) > 120:
        return value[:117] + "..."
    return value


def main() -> int:
    load_dotenv(ROOT / ".env")

    def env(name: str, default: str) -> str:
        return os.environ.get(f"CURFEW_{name}") or os.environ.get(f"NIGHTHAWK_{name}") or default

    host = env("HOST", "192.168.1.1")
    port = int(env("SOAP_PORT", "5043"))
    ssl = env("SOAP_SSL", "true").lower() == "true"
    user = env("USER", "admin")
    password = env("PASSWORD", "")
    if not password:
        print("CURFEW_PASSWORD is empty in .env")
        return 1

    OUT.mkdir(exist_ok=True)
    router = Router(host, port, ssl, user, password)

    t0 = time.monotonic()
    if not router.login():
        return 1
    print(f"login ok in {int((time.monotonic() - t0) * 1000)} ms, cookie {router.cookie.split('=')[0]}=...")

    results: list[Result] = []
    for service, action in ACTIONS:
        t0 = time.monotonic()
        try:
            resp = router.call(service, action)
        except httpx.HTTPError as err:
            results.append(Result(service, action, 0, "error", 0, note=str(err)))
            print(f"  {service:26} {action:32} transport error: {err}")
            continue
        elapsed = int((time.monotonic() - t0) * 1000)
        code = response_code(resp.text)
        (OUT / f"{service.split(':')[0]}_{action}.xml").write_text(resp.text)
        fields = parse_fields(resp.text, action) if code in {"0", "000", "0000"} else {}
        r = Result(service, action, resp.status_code, code, elapsed, fields)
        results.append(r)
        mark = "OK " if r.ok else "-- "
        print(f"{mark} {service:26} {action:32} http {resp.status_code} code {code:>5} {elapsed:5} ms")
        # session may have expired, re-login once and retry this action
        if code == "401" and router.login():
            resp = router.call(service, action)
            results[-1].response_code = response_code(resp.text)
            results[-1].note = "retried after re-login"

    write_summary(results, router.url)
    ok = sum(1 for r in results if r.ok)
    print(f"\n{ok}/{len(results)} actions supported. Summary: {OUT / 'SUMMARY.md'}")
    return 0


def write_summary(results: list[Result], url: str) -> None:
    lines = [
        "# CAX80 SOAP capability matrix",
        "",
        f"Endpoint: `{url}`  ",
        f"Probed: {time.strftime('%Y-%m-%d %H:%M:%S')}  ",
        "Login: SOAP v2 (`DeviceConfig:1#SOAPLogin`, session cookie)",
        "",
        "Legend: `0000` supported, `501` action not found, `404` service not found, "
        "`402` missing parameter, `401` unauthorized.",
        "",
        "| Service | Action | Code | ms | Fields |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        fields = ", ".join(r.fields) if r.fields else ""
        lines.append(f"| {r.service} | {r.action} | {r.response_code} | {r.elapsed_ms} | {fields} |")

    lines += ["", "## Supported action details", ""]
    for r in results:
        if not r.ok or not r.fields:
            continue
        lines.append(f"### {r.service} {r.action}")
        lines.append("")
        for k, v in r.fields.items():
            lines.append(f"- `{k}`: {redact(k, v)}")
        lines.append("")

    (OUT / "SUMMARY.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
