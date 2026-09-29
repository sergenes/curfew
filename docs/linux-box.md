# Leaving curfew running on a Linux box

Schedules only fire while the watcher runs, so it wants a machine that is always on.
On a Mac, `uv run curfew watcher install` runs it as a launchd agent at login, which is fine until the laptop goes to sleep.
A small always-on Linux box is the better home, and these steps are for Rocky/RHEL (`dnf`); on Debian or Ubuntu use `apt`.

Install the toolchain and system deps, then set up the project **under `/opt`, not a home directory**. SELinux (enforcing on Rocky by default) will not let a systemd service execute a binary under `/home`, so a checkout in `/home` fails with `203/EXEC`.

```
sudo dnf install -y git libpcap policycoreutils-python-utils
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
sudo mkdir -p /opt/curfew && sudo chown "$USER":"$USER" /opt/curfew
git clone https://github.com/sergenes/curfew.git /opt/curfew
cd /opt/curfew && uv sync
cp .env.example .env         # fill in CURFEW_HOST, CURFEW_USER, CURFEW_PASSWORD
uv run curfew status         # confirm it reaches the router
```

Give the venv an executable SELinux label so systemd may run it (skip if SELinux is disabled):

```
sudo semanage fcontext -a -t bin_t "/opt/curfew/\.venv/bin(/.*)?"
sudo restorecon -Rv /opt/curfew/.venv/bin
```

Run the watcher as a systemd service so it starts at boot and restarts on failure.
`ExecStart` points at the venv's `curfew` binary, so there is no PATH or `uv` dependency, and `WorkingDirectory` is the checkout so `.env` is found.

```
sudo tee /etc/systemd/system/curfew-watch.service >/dev/null <<'EOF'
[Unit]
Description=Curfew watcher (registry scans and schedule enforcement)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/curfew
ExecStart=/opt/curfew/.venv/bin/curfew watch --interval 60
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now curfew-watch
journalctl -u curfew-watch -f
```

`curfew-watch` is what makes schedules fire: device bedtimes and guest-network schedules are applied on each of its scans.

The services run as **root**, so they use `/root/.curfew` for the registry and pause state. Manage the box with sudo so your commands share that same database, e.g. `sudo /opt/curfew/.venv/bin/curfew pause <mac>`; running the CLI as an unprivileged user would read a different registry the daemons never see.

The per-device daemons from [detours.md](detours.md) run on the same box; their units are in that file.
