# curfew

Local control of a NETGEAR Nighthawk CAX80 cable modem router over its SOAP API.
No cloud account, no subscription, nothing leaves the LAN.

See `PLAN.md` for the design and roadmap and `discovery/SUMMARY.md` for what the firmware supports.

Companion writing:

- Blog post: _coming soon_
- Article: _coming soon_

## Setup

```
uv sync
cp .env.example .env            # then fill in CURFEW_PASSWORD
uv run curfew vendors update # optional: IEEE vendor table so unknown devices show a manufacturer
```

Local state (registry database, vendor table, watcher logs) lives in `~/.curfew/`.
Set `CURFEW_DATA_DIR` to move it.

## CLI

Router:

```
uv run curfew status              # model, firmware, uptime, WAN, LAN, Access Control
uv run curfew wifi                # both bands, channels, security, guest state
uv run curfew log                 # DHCP leases, Access Control decisions, attack warnings
uv run curfew traffic             # traffic meter totals
uv run curfew firmware            # ask the router for updates (about 10 s)
```

Devices and the local registry:

```
uv run curfew devices             # who is attached now, with nicknames, owners, vendors (--sort name|owner|seen)
uv run curfew scan                # refresh the registry, print new / returned / left
uv run curfew new --since 7d      # devices first seen in the window
uv run curfew known --online      # everything ever seen (--owner X, --tag Y)
uv run curfew name "iPhone" --nickname "Sam's phone" --owner Sam --tags kid,phone
uv run curfew name CE:46:A0:00:00:09 --nickname "Sam's iPad" --owner Sam
uv run curfew merge CE:46:A0:00:00:09 30:C0:AE:00:00:11   # one device seen twice: fold the old MAC into the new, keep the name
uv run curfew history "Sam's phone"
uv run curfew watch --interval 60 # keep scanning in the foreground
uv run curfew watcher install     # same, as a launchd agent at login (status / uninstall)
```

Devices are addressed by MAC, nickname, router name or IP.
A `*` after a MAC marks a randomized (private) wifi address; those change, so give the device a nickname.
When a device changes its MAC and shows up as a second entry, `merge` folds the old record into the new one: the new MAC survives and inherits the old nickname, owner, tags and history.

Groups, access on/off, schedules (basic parental control):

```
uv run curfew group add kids "Sam's phone" "Sam's iPad"   # groups are created on first use
uv run curfew group list
uv run curfew access off kids          # block every device in the group (also works for an owner or one device)
uv run curfew access on kids
uv run curfew access status            # what is blocked and why
uv run curfew access clear             # unblock everything curfew has blocked; add a MAC to clear an orphaned entry
uv run curfew schedule add kids --from 21:00 --to 07:00 --days weekdays --name bedtime
uv run curfew schedule list
uv run curfew schedule apply           # evaluate now; the watcher does this after every scan
uv run curfew reboot                   # asks for confirmation
uv run curfew guest on --band both
```

Eclipse Pause (instant cutoff, no router, no subscription):

```
sudo -E uv run curfew eclipse run          # the enforcement daemon (needs root); keep it running
uv run curfew pause kids                    # cut a group/owner/device off the internet in ~1s
uv run curfew resume kids                    # restore it
uv run curfew resume --all
uv run curfew group add admin "My Mac" "Home Server" "Living Room Switch"   # devices that must stay online
uv run curfew alloff                         # cut the internet for everything except the admin group
uv run curfew allon                          # restore everyone
uv run curfew eclipse status                 # daemon state and who is paused
sudo -E uv run curfew eclipse verify "Sam's iPad"   # prove it works on one device, then self-heal
sudo -E uv run curfew eclipse doctor         # show the resolved network context
```

Eclipse Pause is a self-hosted replacement for the paywalled Circle Pause.
It cuts an already-connected device in about a second by ARP interception, works without
touching the router or the device, and keeps the device on wifi and the LAN.
The router's MAC block (below) stays as the persistent, router-level layer; Eclipse is the instant one.
Enforcement runs in the `eclipse` daemon as root; `pause` only records intent, so start the daemon first.
The daemon re-sends every second by default and tracks a device's IP across DHCP changes; for a stubborn
device that keeps recovering, run it tighter with `eclipse run --interval 0.2`.
By default it poisons both directions: it tells the victim the router is at our MAC and tells the router
the victim is at our MAC, so a device cannot recover a working path between re-sends. This is what cuts a
buffering stream without a reboot; pass `--one-way` for victim-only poisoning.
The spoof is a couple of tiny unicast frames per paused device, and it never leaves the LAN, so it costs
no internet bandwidth and adds only negligible wifi airtime even at a fast interval.
It works only while the daemon runs, and only on the machine running it (your Mac now, a Linux mini PC later).

How the two layers behave, worth knowing before you rely on either.
A block prevents a device from joining the network, so it takes effect on the device's next connection, not on a live one.
A pause cuts a device that is already connected, usually in a second or two, but an app that prefetches video far ahead, YouTube Shorts most of all, can keep playing from its buffer for several minutes before it runs dry.
For an immediate and complete stop, do all three: block the device, pause it, then reconnect it by toggling its wifi, which tears down the open streaming sessions and empties the buffer.

`alloff` is the house-wide kill switch: it pauses every attached device except the `admin` group, so put
your own computer, the home server and any always-on devices in `admin` first (`--except <group>` to use a
different one). `allon` lifts every pause at once.

Blocking uses the router's Access Control feature, enabled automatically the first time.
Its default policy for new devices is "allow all", so enabling it changes nothing by itself.
A manual `access on|off` lasts until the target's next scheduled change, or until you flip it back if it has no schedule.
Schedules are enforced by `curfew watch` or the launchd watcher, so install the watcher if you rely on them.
Every change is appended to `~/.curfew/changes.log`.

Every command takes `--json` for machine readable output.

### When a block will not hold

Both blocking layers key on the device's MAC address, so anything that changes that address defeats them.

Apple and Android devices use a randomized (private) wifi MAC by default, shown with a `*` after the MAC in `devices`.
iOS rotates this address periodically and on rejoin, and when it rotates, the router block and Eclipse keep targeting the old MAC, so the device comes back unblocked.
To make a block stick, turn the private address off for your network on the device itself: on iOS go to Settings, Wi-Fi, tap the network, set Private Wi-Fi Address to Off; on Android use the per-network "Privacy" or "MAC address type" setting.

A cellular device can also keep internet after a successful wifi block by falling back to mobile data.
iOS "Wi-Fi Assist" does this automatically the moment wifi stops reaching the internet, so a working block can still look broken.
Neither layer can touch traffic that never crosses your router, so that case is a device setting, not something curfew controls.

## Agent tools (MCP)

`.mcp.json` registers the server with Claude Code when it is started from this directory.
Read tools: `router_status`, `list_devices`, `scan_network`, `who_is_new`, `known_devices`, `device_history`,
`system_log`, `wifi_info`, `traffic_stats`, `check_firmware`, `list_groups`, `access_status`, `list_schedules`.
Registry writes: `name_device`, `merge_devices`, `set_group_membership`, `add_schedule`, `remove_schedule`.
Router writes: `set_access` (group, owner or device on/off), `clear_access_control` (empty the deny list), `apply_schedules`, `set_guest_wifi`, `reboot_router` (needs `confirm=true`).
Eclipse Pause: `pause_device`, `pause_all_except` (cut everything but a group), `resume_device`, `list_paused` (instant ARP cutoff; enforced by the `eclipse` daemon).

Run it by hand with `uv run curfew-mcp` (stdio).

## Development

```
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                        # unit tests on recorded fixtures and a temp registry
uv run pytest -m live                # smoke test against the real router
uv run python scripts/discover.py    # re-probe the router, e.g. after a firmware update
uv run python scripts/make_fixtures.py   # refresh sanitized fixtures from discovery dumps
```

### Firmware baseline and what to do after an update

The CAX80 updates its firmware on its own, and NETGEAR can change SOAP response layouts between versions.
`tests/fixtures/` holds real responses recorded from firmware V5.1.1.8 with MACs, SSIDs, serial and public
addresses replaced by synthetic values. They are the baseline: the parsers are known to work on exactly
these shapes, and `uv run pytest` proves it in a few seconds without touching the router.

When a command starts failing after a firmware update:

1. `uv run python scripts/discover.py` re-probes every SOAP action and rewrites `discovery/SUMMARY.md`.
   Diff the summary against the committed one to see which actions changed, appeared or vanished.
2. Fix the parsers in `curfew/actions.py`. They fail loudly and include the raw XML in the error.
3. `uv run python scripts/make_fixtures.py` regenerates the fixtures from the new dumps, sanitized.
4. `uv run pytest` until green, then commit the new fixtures and summary as the new baseline.

`uv run pytest -m live` is the quick check that the real router still agrees with the fixtures.

## Layout

```
curfew/soap.py          async SOAP client: login, session refresh, config mode
curfew/actions.py       one function per router action, typed results
curfew/models.py        pydantic models
curfew/registry.py      SQLite device registry: presence sessions, groups (tags), schedules, overrides
curfew/scheduler.py     rule evaluation in local time, manual override precedence
curfew/pause.py         Eclipse Pause: ARP frame building, engine, daemon heartbeat
curfew/vendor.py        OUI vendor lookup, randomized MAC detection
curfew/services/        compositions: status, wifi, devices, control, eclipse (ARP pause)
curfew/mcp_server.py    MCP server (stdio)
curfew/cli.py           typer CLI
scripts/                   discovery and fixture tooling
tests/                     respx fake router, recorded fixtures, temp registries
```

## License and author

MIT, see `LICENSE`.
Sergey Neskoromny.
Reach me on [LinkedIn](https://www.linkedin.com/in/sergey-neskoromny/).
