# Curfew: CAX80 Agent Toolkit, Project Plan

Goal: a small Python toolkit that lets an AI agent (Claude Code, or any MCP client) manage the home router at `http://192.168.1.1`.
Target device: NETGEAR Nighthawk CAX80, firmware V5.1.1.8.
Everything runs on the LAN, talks directly to the router, and needs no NETGEAR cloud account or paid subscription.

## 1. Feasibility: what the router already exposes

A read-only probe on 2026-09-12 showed the following.

| Probe | Result |
|---|---|
| `http://192.168.1.1/` | HTTP Basic auth, realm `NETGEAR Cable`, plus an `XSRF_TOKEN` cookie |
| `http://192.168.1.1/currentsetting.htm` | Unauthenticated discovery file (see below) |
| `http://192.168.1.1:5000/soap/server_sa/` | 404, the classic SOAP port is not used on this firmware |
| `https://192.168.1.1:5043/soap/server_sa/` | Valid SOAP envelope, `ResponseCode 401` until login |

Contents of `currentsetting.htm`:

```
Firmware=V5.1.1.8
Model=CAX80
SOAPVersion=3.21
LoginMethod=2.0
ParentalControlSupported=0
XCloudSupported=1
SOAP_HTTPs_Port=5043
```

Conclusions:

- The router speaks the NETGEAR SOAP API (the same protocol the Nighthawk mobile app and Home Assistant use), over HTTPS on port 5043, with the v2 login method.
- This is exactly what the open source `pynetgear` library implements, so the answer to "is it possible in Python" is yes.
- `ParentalControlSupported=0` means the Circle / OpenDNS style parental control is not on this firmware.
  "Basic parental control" will therefore be built from per-device block/allow (Access Control) plus a scheduler on our side.
- The web UI is a fallback data source for anything SOAP does not cover (for example DOCSIS signal levels).

## 1b. Phase 0 results (2026-09-12)

`scripts/discover.py` logged in over SOAP v2 and probed 49 read-only actions.
The generated matrix is in `discovery/SUMMARY.md`, raw responses in `discovery/*.xml` (gitignored, they contain wifi passphrases).

Supported (35 actions), the ones that matter:

- `DeviceInfo:1` GetInfo, GetSysUpTime, GetSystemInfo (CPU, memory, flash), GetAttachDevice2 (25 devices, rich fields), GetSystemLogs (DHCP leases and Access Control events with timestamps), GetSupportFeatureListXML.
- `DeviceConfig:1` GetInfo, GetTimeZoneInfo, GetTrafficMeter{Enabled,Options,Statistics}, GetBlockDeviceEnableStatus (Access Control exists, currently off), GetBlockSiteInfo, CheckNewFirmware (slow, about 10 s).
- `WANIPConnection:1` GetInfo (external IP, gateway, DNS), GetConnectionTypeInfo, GetPortMappingInfo.
- `WANEthernetLinkConfig:1` GetEthernetLinkStatus.
- `LANConfigSecurity:1` GetInfo.
- `WLANConfiguration:1` GetInfo, Get5GInfo, GetChannelInfo, Get5GChannelInfo, GetRegion, GetWPASecurityKeys, Get5GWPASecurityKeys, GetGuestAccessEnabled, Get5GGuestAccessEnabled, GetGuestAccessNetworkInfo, Get5GGuestAccessNetworkInfo, IsSmartConnectEnabled.
- `ParentalControl:1` GetEnableStatus, GetAllMACAddresses (both empty, OpenDNS parental control is not configured).

Not supported on this firmware: the whole `AdvancedQoS:1` service (501), so no speed test or QoS through SOAP.
Also 501: GetDeviceListAll, GetAllSatellites, GetDeviceConfig, the R8000 style guest variants, GetWLANMACAddress.

Per-device fields in GetAttachDevice2: IP, Name, NameUserSet, MAC, ConnectionType (wired / 2.4GHz / 5GHz), SSID, Linkspeed, SignalStrength, AllowOrBlock, Schedule, DeviceType, DeviceTypeV2, DeviceTypeNameV2, Upload, Download, QosPriority, DeviceModel, Grouping, SchedulePeriod.
The `Schedule` and `SchedulePeriod` fields hint at router-side per-device schedules, worth probing in Phase 3.

Feature list advertised by the router: AccessControl 1.0, AttachedDevice 2.0, SpeedTest 2.0, DynamicQoS 1.0, GuestNetworkSchedule 1.0, NewDeviceNoti 1.0, DeviceTagging 1.0, NameNTGRDevice 1.0, CircleParentalControlV2 1.0 (the paid one), BitDefenderSecurity 1.0 (Armor, paid), CableModem 1.0.

Write path verified without changing anything: `ConfigurationStarted` then `ConfigurationFinished` both return 000, and the session survives the cycle.

Session behaviour: login takes about 100 ms, a second login returns the same `sess_id` cookie, and the earlier cookie stays valid.
Reading GetAttachDevice2 takes about 4 s and GetAttachDevice about 8 s, so the poller should not run more often than once a minute.

Consequences for the plan:

- `speed_test` is dropped from the SOAP tool list; if wanted later it goes through the web UI.
- New tool `system_log`: recent DHCP and Access Control events straight from the router.
- New tool `wan_status`: external IP, gateway, DNS servers, cable link state.
- Access Control is available, so `block_device` and `unblock_device` proceed as designed.

## 2. Architecture

```
curfew/
  soap.py          thin async SOAP client (login, session, one call per action)
  actions.py       catalog of SOAP services and actions with typed parsers
  models.py        pydantic models: Device, RouterInfo, TrafficStats, ...
  registry.py      SQLite device registry: first/last seen, nickname, owner, tags
  services/
    devices.py     list, diff, presence, name resolution
    control.py     block/unblock, schedules, reboot, guest wifi
    status.py      router info, WAN, traffic meter, speed test
  scheduler.py     time-based rules (bedtime blocks) evaluated by a background loop
  webui.py         optional: Basic-auth scraper for pages SOAP lacks
  mcp_server.py    FastMCP server exposing the tools to agents
  cli.py           typer CLI for humans and for quick manual checks
tests/
  fixtures/        recorded SOAP responses from the real router
discovery/         raw dumps from Phase 0 (gitignored except the summary)
PLAN.md
```

Design decisions:

- **Own thin SOAP client instead of depending on `pynetgear` at runtime.**
  We need roughly 15 actions, the port and login method are already known, and we want async, typed responses, and clear errors.
  `pynetgear` and `pynetgear_enhanced` are used as the reference catalog of service names, action names, and request bodies.
- **MCP server as the agent interface.**
  Claude Code can then call `list_devices`, `block_device`, etc. as native tools.
  Transport is stdio, local only, so nothing is exposed on the network.
- **A local SQLite registry is the memory.**
  The router only knows the current attached devices.
  "Who is new on my network" and "when did the kids' tablet last connect" need history that we keep ourselves.
- **Write actions are explicit and guarded.**
  Reboot, block, and schedule changes go through one `control` module that logs every change and refuses ambiguous device references.

Stack: Python 3.12, `uv`, `httpx`, `pydantic`, `typer`, `mcp` (FastMCP), stdlib `sqlite3`, `pytest` + `respx`, `ruff`, `mypy`.
Credentials live in `.env` (gitignored) or the macOS Keychain via `keyring`, never in code or logs.

## 3. Tools exposed to the agent

| Tool | Purpose | Backing SOAP action (expected) |
|---|---|---|
| `router_status` | Model, firmware, serial, uptime, WAN/internet state | `DeviceInfo#GetInfo`, `DeviceInfo#GetSysUpTime`, `WANIPConnection#GetInfo` |
| `list_devices` | Everyone currently on the network: name, IP, MAC, wired/2.4G/5G, link rate, signal, blocked or allowed | `DeviceInfo#GetAttachDevice2` |
| `who_is_new` | Devices first seen since a given time, unknown devices | registry diff |
| `device_history` | First seen, last seen, connection timeline for one device | registry |
| `name_device` | Assign nickname, owner, tags (`kid`, `iot`, `guest`) | registry |
| `block_device` / `unblock_device` | Cut a device off or restore it | `DeviceConfig#SetBlockDeviceByMAC`, `SetBlockDeviceEnable` |
| `set_schedule` | Bedtime style rules: block a device or an owner group on a daily time window | our scheduler + block actions |
| `list_schedules` / `remove_schedule` | Inspect and remove rules | scheduler |
| `reboot_router` | Reboot with confirmation | `DeviceConfig#Reboot` inside `ConfigurationStarted/Finished` |
| `traffic_stats` | Today / week / month up and down totals | `DeviceConfig#GetTrafficMeterStatistics` |
| `wan_status` | External IP, gateway, DNS servers, cable link state | `WANIPConnection#GetInfo`, `WANEthernetLinkConfig#GetEthernetLinkStatus` |
| `system_log` | Recent DHCP leases and Access Control events with timestamps | `DeviceInfo#GetSystemLogs` |
| `speed_test` (dropped) | `AdvancedQoS` is not on this firmware; web UI only if ever needed | none |
| `guest_wifi` | Get or set guest network state for 2.4G and 5G | `WLANConfiguration#GetGuestAccessEnabled`, `SetGuestAccessEnabled` |
| `wifi_info` | SSIDs, channels, security mode | `WLANConfiguration#GetInfo`, `Get5GInfo` |
| `check_firmware` | Is a newer firmware available | `DeviceConfig#CheckNewFirmware` |
| `docsis_status` (optional) | Downstream/upstream power, SNR, uncorrectables | web UI scrape of `DocsisStatus.htm` |

Every tool returns structured JSON so the agent can reason over it, and every write tool returns what changed.

## 4. Phases

### Phase 0: Discovery (done 2026-09-12, see Section 1b)

Goal: learn exactly which SOAP actions this cable firmware supports before writing product code.

1. Write `scripts/discover.py`: log in over `https://192.168.1.1:5043` with the v2 method, then call every known `Get*` action from the `pynetgear` and `pynetgear_enhanced` catalogs.
2. Save each raw response to `discovery/<service>_<action>.xml` and produce `discovery/SUMMARY.md` with a supported / unsupported / needs-config-mode matrix.
3. Probe `GetAttachDevice2` carefully: it is the single most important call, and field layout differs between firmwares.
4. Check whether Access Control (`GetBlockDeviceEnableStatus`) exists on this firmware, because it decides how parental control is implemented.
5. Test one harmless write in `ConfigurationStarted/Finished` mode (for example re-setting the current traffic meter option) to confirm write calls work.
6. Note the session behaviour: does a SOAP login log out the web UI session, how long does a session live, is there a concurrent session limit.

Exit criteria: a capability matrix and recorded fixtures for every supported action.

### Phase 1: Core client and read-only tools (done 2026-09-12)

1. `soap.py`: async client with login, session cookie, one automatic re-login on `ResponseCode 401`, typed `SoapError`, `config_mode()` context manager for writes.
2. `actions.py` + `models.py` for router info, uptime, system load, attached devices, system log, traffic, WAN, LAN, wifi, firmware check.
3. `cli.py`: `status`, `devices`, `wifi`, `log`, `traffic`, `firmware`, each with `--json`.
4. 17 unit tests on sanitized fixtures (`scripts/make_fixtures.py` builds them from discovery dumps) plus one opt-in live test.

Notes from the live run:

- Calls are serialized through one lock; the router is a small embedded box and `GetAttachDevice2` alone takes about 4 s.
- `NewCPUUtilization` reports values above 100 (227, 247), most likely summed across cores; shown raw for now.
- `Linkspeed` and `SignalStrength` return identical values for every device, so the CLI shows signal only.
- The router log mixes DHCP leases, Access Control decisions, and "DoS attack" scan warnings; the parser normalizes them into category, message, timestamp.

### Phase 2: Device registry and MCP server (done 2026-09-12)

1. `registry.py`: SQLite at `~/.curfew/registry.db` (override with `CURFEW_DATA_DIR`).
   Tables: `devices` (first/last seen, last IP, name, band, type, model, vendor, nickname, owner, tags, notes), `sessions` (presence stretches with start, last seen, end), `scans`.
   Instead of one row per sighting, a device keeps one open session while it stays attached; the session closes after 3 consecutive missed scans, because the CAX80 drops sleeping devices from a listing now and then.
2. `vendor.py`: IEEE OUI table downloaded by `curfew vendors update` (the IEEE site needs a browser user agent), plus randomized MAC detection.
   Many phones use private wifi addresses, so their MAC changes per network and sometimes per day; the CLI marks them with `*` and the MCP instructions tell the agent to prefer nicknames.
3. `services/devices.py`: live listing merged with the registry, scan deltas (new, returned, left), strict resolution by MAC, nickname, router name or IP with an explicit ambiguity error.
4. `mcp_server.py` (MCP SDK 2.x, stdio): `router_status`, `list_devices`, `scan_network`, `who_is_new`, `known_devices`, `device_history`, `system_log`, `wifi_info`, `traffic_stats`, `check_firmware`, and the only writer so far, `name_device` (registry only).
   Registered in `.mcp.json`; verified with a real stdio client handshake.
5. CLI: `devices` now shows nickname, owner, vendor and first seen; new commands `scan`, `new`, `known`, `name`, `history`, `watch`, `vendors update`, `watcher install|uninstall|status` (launchd agent, not installed by default), `mcp`.

Exit criteria met: 36 unit tests, live smoke test, MCP handshake, registry persisted across runs.

### Phase 3: Control, groups and basic parental control (done 2026-09-12)

1. Groups: a group is a tag in the registry (`kids`, `parents`, `iot`), so one device can be in several groups and groups can span owners.
   CLI `group list|add|remove`, MCP `list_groups`, `set_group_membership`.
2. Access on/off for a target, where a target is resolved as group first, then owner, then single device.
   CLI `access off kids`, `access on kids`, `access status`, `access control on|off`; MCP `set_access`, `access_status`.
   Blocking uses the router's Access Control (`SetBlockDeviceByMAC`); the feature is enabled automatically on first use.
   The router's default policy for new devices was read from `AccessControl.htm` and is "allow all", so enabling it blocks nothing by itself.
3. Schedules in `scheduler.py`: rules `{kind, target, start, end, days}` evaluated in local time, windows may cross midnight.
   A manual on/off is an override that lasts until the target's next scheduled boundary, or until flipped back when the device has no schedule.
   `apply_schedules` runs after every scan in `watch`, and on demand via `schedule apply`; it is idempotent.
   CLI `schedule add|list|remove|apply`; MCP `add_schedule`, `list_schedules`, `remove_schedule`, `apply_schedules`.
4. `reboot` (CLI asks for confirmation, MCP needs `confirm=true`) and `guest on|off --band 2.4|5|both`; MCP `reboot_router`, `set_guest_wifi`.
5. Every change is appended to `~/.curfew/changes.log`.

Live verification: blocking the Roomba flipped its `AllowOrBlock` to Block on the router within about 6 seconds (a 3 second check was too early, the listing lags), and unblocking restored it.
Not verified live: reboot and guest wifi toggles (unit tested against the fake router only).
Router side per-device schedule fields (`Schedule`, `SchedulePeriod`) were left alone; our scheduler covers the need without depending on undocumented actions.

Known quirk: the router clock runs one hour behind local time (time zone -5 with daylight saving off), so router log timestamps are off by an hour.

### Phase 4: Extras (optional)

1. `webui.py` scraper for `DocsisStatus.htm` (cable signal health) and, if SOAP lacks it, keyword site blocking.
2. Telegram alerts through the existing `notify` skill: "unknown device joined", "internet down", "DOCSIS signal degraded".
3. Firmware check on a weekly schedule.
4. Home Assistant is deliberately out of scope; if ever wanted, the same client can back a custom integration.

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Cable firmware supports fewer SOAP actions than the classic router firmware | Phase 0 measures this first; the web UI scraper is the fallback |
| Access Control absent via SOAP | Fall back to the web UI form for block/allow, keep the same tool interface |
| SOAP login evicts the browser admin session, or sessions are limited | Reuse one session, log out cleanly, poll no more often than every 60 s |
| Firmware auto-updates change response layouts | Fixtures pin the current layout; parsers fail loudly and include the raw XML in the error |
| Wrong device blocked | Tools accept nicknames or MACs only, never fuzzy names; ambiguous input returns candidates instead of acting |
| Credentials leak into logs or the repo | `.env` is gitignored, password is redacted in every log line, tests use fake credentials |

## 6. Open questions

1. Admin password: store it in `.env` for now, or in the macOS Keychain from day one.
2. Which devices belong to whom, so the registry can be seeded with owners and the parental rules have targets.
3. Do you want Telegram alerts for new devices from the start, or only after the registry is trusted.
4. Poll interval for the background watcher: 60 seconds gives good presence detection at negligible router load.

## 7. References

- pynetgear (SOAP catalog and login flow): https://github.com/MatMaul/pynetgear
- pynetgear_enhanced (extra actions: speed test, guest wifi, QoS): https://github.com/roblandry/pynetgear_enhanced
- Home Assistant NETGEAR integration (feature availability notes): https://www.home-assistant.io/integrations/netgear/
- CAX80 user manual: https://www.downloads.netgear.com/files/GDC/CAX80/CAX80_UM_EN.pdf
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk

### Phase 4a: Eclipse Pause (done 2026-09-12)

The router's MAC Access Control only enforces at (re)association, so it does not cut a device
that is already connected; the instant "pause a live device" capability is paywalled behind
Circle, which uses ARP interception. Eclipse Pause reimplements that, self-hosted and free.

- `pause.py`: pure ARP reply frame builder (Ethernet + ARP, unit tested via `parse_arp_reply`),
  `NetworkInfo`, a `Sender` protocol with a scapy backend (macOS and Linux), `PauseEngine`
  (spoof, heal, safety refusing the router and this host), target diffing, and a heartbeat file.
- `services/eclipse.py`: `EclipseService` records the paused set in the registry (`pauses` table),
  and `run_daemon` enforces it: re-sends the spoof every ~2s, refreshes device IPs from the router
  every ~60s, heals removed and all-on-exit devices, writes a heartbeat.
- CLI: `pause`, `resume [--all]`, `eclipse run|status|doctor|verify`.
- MCP: `pause_device`, `resume_device`, `list_paused`.

Mechanism: tell the target that the router's IP is at our MAC, so its internet-bound traffic comes
to us and is dropped. Device stays on wifi and reachable on the LAN, like Circle's pause.

Design notes:
- The MAC block stays as the persistent, router-level layer; Eclipse is the instant, host-local one.
- Enforcement needs root (raw ARP) and only works while the daemon runs on a LAN host.
- scapy is the send/resolve backend for both platforms; the `Sender` protocol leaves room for a
  zero-dependency Linux AF_PACKET backend later, for the eventual Linux mini PC.
- Not runnable in this session (root required); packet building, safety, engine, service and the
  MCP tools are unit tested (66 tests). Live proof is `sudo -E uv run curfew eclipse verify <device>`.

Next: run `eclipse verify` on one device to confirm on your network, then install the daemon
(launchd now, systemd on the Linux box). A native AF_PACKET backend and Telegram alerts remain optional.
