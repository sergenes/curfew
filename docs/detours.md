# The detours

Before the guest network turned out to be the answer, curfew grew three per-device layers: blocking at the router, an instant pause, and a website filter, plus a registry that remembers every device in the house.
They all still work, and they are still handy when you need to single out one device on the main network.
You do not need any of them for the setup in the [README](../README.md).
The [article](https://medium.com/gitconnected/the-case-of-the-youtube-shorts-that-survived-every-parental-control-a-home-wi-fi-mystery-in-three-43df64011dfe) tells the story of why.

## Per-device quick start

```
uv run curfew devices                       # who is online right now
uv run curfew name "iPhone" --nickname "Sam phone" --owner Sam --tags kids

uv run curfew access off kids               # block a device/owner/group at the router (persists across reboots)
uv run curfew access on kids                # unblock

uv run curfew pause "Sam phone"             # cut it off this second (needs the eclipse daemon, below)
uv run curfew resume "Sam phone"
uv run curfew alloff                         # cut the whole house except the "admin" group
uv run curfew allon

uv run curfew filter mode kids blacklist    # website filtering per group/device (needs the dns daemon, below)
uv run curfew filter block kids youtube.com tiktok.com

uv run curfew schedule add kids --from 21:00 --to 07:00 --days weekdays --name bedtime
```

Two background daemons power the per-device instant layers, each run once and left running (root):

```
sudo -E uv run curfew eclipse run           # enforces pause / alloff (ARP)
sudo -E uv run curfew dns run               # enforces the website filter (DNS)
```

Ask an agent instead of typing: with the MCP server connected, "pause Sam's phone" or "block YouTube for the kids" just works.

## Devices and the local registry

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

## Groups, blocking and device schedules

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
```

Blocking uses the router's Access Control feature, enabled automatically the first time.
Its default policy for new devices is "allow all", so enabling it changes nothing by itself.
A manual `access on|off` lasts until the target's next scheduled change, or until you flip it back if it has no schedule.
Every change is appended to `~/.curfew/changes.log`.

## Eclipse Pause

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

## When a block will not hold

Both blocking layers key on the device's MAC address, so anything that changes that address defeats them.

Apple and Android devices use a randomized (private) wifi MAC by default, shown with a `*` after the MAC in `devices`.
iOS rotates this address periodically and on rejoin, and when it rotates, the router block and Eclipse keep targeting the old MAC, so the device comes back unblocked.
To make a block stick, turn the private address off for your network on the device itself: on iOS go to Settings, Wi-Fi, tap the network, set Private Wi-Fi Address to Off; on Android use the per-network "Privacy" or "MAC address type" setting.

A cellular device can also keep internet after a successful wifi block by falling back to mobile data.
iOS "Wi-Fi Assist" does this automatically the moment wifi stops reaching the internet, so a working block can still look broken.
Neither layer can touch traffic that never crosses your router, so that case is a device setting, not something curfew controls.

## Website filtering (DNS)

A third layer blocks or allows websites per group, owner or device, without touching the router's own settings.
It runs a small DNS resolver on the always-on box; each lookup arrives tagged with the asking device's IP, so a group or one device can have its own policy while everyone else is untouched.

```
sudo -E uv run curfew dns run                 # the DNS filter daemon (needs root for port 53); keep it running
uv run curfew filter mode kids blacklist      # block the block list for the kids group
uv run curfew filter block kids youtube.com tiktok.com
uv run curfew filter mode "Sam's iPad" whitelist   # a stricter per-device policy overrides the group
uv run curfew filter allow "Sam's iPad" school.edu wikipedia.org
uv run curfew filter list kids                # modes and lists
uv run curfew filter status                   # is the daemon running, which scopes are filtered
uv run curfew dns status
```

Two modes per scope: `blacklist` refuses the listed domains and forwards the rest; `whitelist` forwards only the listed domains and refuses everything else.
The most specific scope with a mode set wins, in the order device, owner, group, then a global default, so you can filter a whole group and still exempt one device.
Domains match by suffix, so `youtube.com` also covers `www.youtube.com`.
A device that is paused (Eclipse) or blocked (Access Control) resolves nothing: the DNS filter refuses all of its lookups, so a single block bites at the router, the ARP layer, and DNS together.
Each client's policy is cached for a few seconds, so a new pause, block or rule takes effect within that window rather than on the very next lookup.

One-time setup: point the router's DHCP DNS at the machine running the daemon, so every device resolves through it.
Honest limits: it filters by domain, so it is all-YouTube not just Shorts; a device using its own encrypted DNS (DoH) can route around it unless you also block that; and it cannot stop video already sitting in a device's buffer.
IP entries are stored but only a future gateway mode enforces them; the DNS filter matches domains.

### Pointing devices at the filter

For the filter to see a device's lookups, that device has to use the box as its DNS server. There are two ways, depending on your router.

If the router lets you set the DNS handed out by DHCP (usually under LAN Setup / DHCP, a field separate from the WAN DNS), set it to the box's IP. Every device then uses the filter on its next lease.

Many routers, the NETGEAR CAX80 included, have no such field: LAN Setup only has the DHCP range, not a client DNS server. Do not work around this by changing the router's own DNS (the "Domain Name Server Address" under Internet Setup). That is the router's upstream resolver, and pointing it at a LAN box makes the router's connectivity check fail, so the whole house loses internet. Leave it on "get automatically from ISP".

Instead, set the DNS on each device you want filtered. On iOS: Settings, Wi-Fi, tap the network, Configure DNS, Manual, remove the existing entries, add the box's IP, Save. On Android it is under the network's IP settings, and on a laptop under the adapter's DNS settings. This targets exactly the devices you care about, works with any router, gives the filter each device's real IP so per-device rules apply, and cannot take down the house if the box is off. Pair it with the device's parental controls (iOS Screen Time) so the DNS setting cannot be changed back.

## Agent tools for these layers

Eclipse Pause: `pause_device`, `pause_all_except` (cut everything but a group), `resume_device`, `list_paused` (instant ARP cutoff; enforced by the `eclipse` daemon).
Website filter: `set_filter_mode`, `set_filter_rule` (add or remove block/allow domains), `list_filters` (enforced by the `dns` daemon).

## Running the daemons on a Linux box

Set the box up as in [linux-box.md](linux-box.md) first, then add the two daemons.

The DNS filter answers other devices, so open its port and check nothing else already holds it:

```
sudo firewall-cmd --add-port=53/udp --permanent && sudo firewall-cmd --reload
sudo ss -ulpn | grep ':53' || true   # if systemd-resolved is listening, free it before starting the filter
```

Run both as systemd services so they start at boot and restart on failure. `ExecStart` points at the venv's `curfew` binary, so there is no PATH or `uv` dependency, and `WorkingDirectory` is the checkout so `.env` is found. Set `--iface` to your LAN interface (from `ip -br a`, e.g. `eno1`).

```
sudo tee /etc/systemd/system/curfew-eclipse.service >/dev/null <<'EOF'
[Unit]
Description=Curfew Eclipse Pause (ARP enforcement)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/curfew
ExecStart=/opt/curfew/.venv/bin/curfew eclipse run --interval 0.2 --iface eno1
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/curfew-dns.service >/dev/null <<'EOF'
[Unit]
Description=Curfew DNS website filter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/curfew
ExecStart=/opt/curfew/.venv/bin/curfew dns run
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now curfew-eclipse curfew-dns
journalctl -u curfew-eclipse -u curfew-dns -f
```
