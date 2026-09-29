# curfew

A dinner bell for your home Wi-Fi.

curfew turns the internet off for the kids' devices and leaves the rest of the house alone: the servers, the cameras, the thermostat, and whatever else in your home has decided it needs Wi-Fi.
The trick is not clever. Put the kids on the router's guest network, then switch that network off with one command, on a schedule, or by asking an AI agent.
It talks to the router directly over the local network. No cloud account, no subscription.

How it got here, including the two weeks spent building the wrong thing first: [The Case of the YouTube Shorts That Survived Every Parental Control](https://medium.com/gitconnected/the-case-of-the-youtube-shorts-that-survived-every-parental-control-a-home-wi-fi-mystery-in-three-43df64011dfe).

## Will it work on my router?

It is built and tested on a NETGEAR Nighthawk CAX80, firmware V5.1.1.8.
Other NETGEAR routers speak the same kind of local API, so many commands may just work, but I have only tested mine.
To see what yours supports, run the discovery script (see [Hacking on it](#hacking-on-it)); it probes every action read-only and writes a report.

No NETGEAR? The idea still works: most routers' own apps can switch the guest network on and off. You just do it by hand.

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Try it in five minutes

```
git clone https://github.com/sergenes/curfew.git && cd curfew
uv sync
cp .env.example .env              # set CURFEW_PASSWORD, your router's admin password
uv run curfew status              # model, firmware, uptime: proof it can reach the router
uv run curfew wifi                # both bands, including whether the guest network is on
```

Then the main event.
Warn the household first, because you will be asked questions:

```
uv run curfew guest off           # guest Wi-Fi off on both bands
uv run curfew guest on
```

> **Heads up: the main Wi-Fi blinks too.**
> The router applies any guest network change by restarting its wireless radios, and the guest and main networks share those radios.
> So every `guest on` or `guest off`, whether you run it or a schedule does, knocks everyone off Wi-Fi for 30 to 60 seconds, not just the kids.
> Devices reconnect on their own, but calls drop and streams stall.
> This is the router's firmware, not curfew: flipping the guest network in NETGEAR's own web page does the same.
> Wired devices are not affected.

## The setup that works

1. Turn on the router's guest network and give it a name the kids will recognise.
2. Move the kids' phones, tablets, and laptops onto it. The TVs too, if screens in the living room count.
3. Keep everything else, the smart home included, on the main network.
4. Keep the main Wi-Fi password to yourself. This is the hardest step.

It does not care which device is which, so the private, rotating MAC addresses on iPhones and iPads cannot slip past it, and there is no device list to maintain.

## Bedtime on a schedule

```
uv run curfew guest schedule --from 21:00 --to 07:00 --name kids-wifi-night
uv run curfew guest schedule --from 22:00 --to 06:30 --days weekdays --band 5
uv run curfew schedule list       # shows "guest wifi off both 21:00-07:00", active or idle
uv run curfew schedule remove <id>
```

Schedules fire from the watcher, so it has to be running:
`uv run curfew watch` in a terminal, `uv run curfew watcher install` to run it at login on a Mac, or a systemd service on an always-on Linux box ([docs/linux-box.md](docs/linux-box.md)).

A schedule acts only at the edges of its window.
If Friday is movie night and you run `guest on` at nine thirty, it stays on until the next boundary, and the schedule does not argue.

## Ask an agent instead

curfew ships an MCP server, so any MCP-capable agent can run it for you.
`.mcp.json` registers it with Claude Code when you start Claude Code in this folder; elsewhere, run `uv run curfew-mcp` (stdio).

Then just say it: "turn off the kids' Wi-Fi", "no Wi-Fi for the kids after ten on school nights", "what schedules are set?"
The tools behind those are `set_guest_wifi`, `add_guest_schedule`, `list_schedules`, and `remove_schedule`, alongside read-only ones like `router_status`, `wifi_info`, and `list_devices`.

## What it cannot do

- **Mobile data.** A phone on a data plan switches to cellular when the Wi-Fi goes. The router has no say; your carrier might (on Google Fi, the plan owner can pause a member's data from the Fi app).
- **Downloaded video.** Shorts already on the device keep playing until they run out. The internet is gone; the buffer is not. Give it a minute.
- **A leaked password.** If the kids know the main Wi-Fi password, they know the way around. See step 4.

## Poke around

```
uv run curfew status              # model, firmware, uptime, WAN, LAN, Access Control
uv run curfew wifi                # both bands, channels, security, guest state
uv run curfew devices             # who is attached right now
uv run curfew log                 # DHCP leases, Access Control decisions, attack warnings
uv run curfew traffic             # traffic meter totals
uv run curfew firmware            # ask the router for updates (about 10 s)
```

Every command takes `--json`, and every change curfew makes is appended to `~/.curfew/changes.log`.
Local state lives in `~/.curfew/`; set `CURFEW_DATA_DIR` to move it.

## The detours

Before the guest network turned out to be the answer, curfew grew a device registry, per-device blocking at the router, an instant per-device pause, and a DNS website filter.
They still work, and they are useful when you need to single out one device on the main network.
Everything about them is in [docs/detours.md](docs/detours.md).

## Hacking on it

```
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                        # recorded router responses, no router needed
uv run pytest -m live                # smoke test against the real router
uv run python scripts/discover.py    # probe every router action read-only, write discovery/SUMMARY.md
uv run python scripts/make_fixtures.py   # refresh sanitized fixtures from discovery dumps
```

The tests run against real responses recorded from firmware V5.1.1.8, with MACs, SSIDs, serial and public addresses replaced by synthetic values, so you can change the code without touching a router.
The router updates its own firmware, and NETGEAR can change response layouts between versions.
When a command breaks after an update: re-run `discover.py` and diff `discovery/SUMMARY.md`, fix the parser in `curfew/actions.py` (errors include the raw XML), regenerate fixtures with `make_fixtures.py`, and run `pytest` until green.

The code, briefly:

```
curfew/soap.py          async SOAP client: login, session refresh, config mode
curfew/actions.py       one function per router action, typed results
curfew/registry.py      SQLite device registry, groups, schedules, overrides
curfew/scheduler.py     rule evaluation in local time, manual override precedence
curfew/services/        compositions: status, wifi, devices, control, and the detours
curfew/mcp_server.py    MCP server (stdio)
curfew/cli.py           typer CLI
scripts/                discovery and fixture tooling
tests/                  fake router, recorded fixtures, temp registries
```

Design notes and the roadmap are in `PLAN.md`; what the firmware supports is in `discovery/SUMMARY.md`.

## Companion writing

- Article: [The Case of the YouTube Shorts That Survived Every Parental Control: A Home Wi-Fi Mystery in Three Suspects](https://medium.com/gitconnected/the-case-of-the-youtube-shorts-that-survived-every-parental-control-a-home-wi-fi-mystery-in-three-43df64011dfe)
- LinkedIn post: _coming soon_

## License and author

MIT, see `LICENSE`.
Sergey Neskoromny.
Reach me on [LinkedIn](https://www.linkedin.com/in/sergey-neskoromny/).
