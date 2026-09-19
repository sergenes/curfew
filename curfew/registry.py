"""SQLite registry of every device ever seen on the network.

The router only knows who is attached right now. This module remembers first and last
sightings, presence sessions (continuous stretches online), and the names, owners and
tags the household assigns. All timestamps are stored as ISO 8601 UTC strings.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curfew.models import (
    AccessOverride,
    Device,
    KnownDevice,
    PauseTarget,
    PresenceSession,
    Rule,
    ScanDelta,
)
from curfew.vendor import is_randomized, normalize_mac

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac          TEXT PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    last_ip      TEXT NOT NULL DEFAULT '',
    last_name    TEXT NOT NULL DEFAULT '',
    last_band    TEXT NOT NULL DEFAULT '',
    last_type    TEXT NOT NULL DEFAULT '',
    last_model   TEXT NOT NULL DEFAULT '',
    vendor       TEXT NOT NULL DEFAULT '',
    nickname     TEXT NOT NULL DEFAULT '',
    owner        TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    absent_scans INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY,
    mac          TEXT NOT NULL REFERENCES devices(mac),
    started_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at     TEXT,
    ip           TEXT NOT NULL DEFAULT '',
    band         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_mac_started ON sessions(mac, started_at);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(mac) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS scans (
    id         INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    present    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    target  TEXT NOT NULL,
    start   TEXT NOT NULL,
    end     TEXT NOT NULL,
    days    TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS overrides (
    mac     TEXT PRIMARY KEY REFERENCES devices(mac),
    allow   INTEGER NOT NULL,
    set_at  TEXT NOT NULL,
    until   TEXT,
    reason  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pauses (
    mac    TEXT PRIMARY KEY REFERENCES devices(mac),
    ip     TEXT NOT NULL DEFAULT '',
    since  TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS filter_modes (
    scope_kind  TEXT NOT NULL,   -- 'global' | 'group' | 'owner' | 'device'
    scope_value TEXT NOT NULL,   -- tag / owner / MAC; '' for global
    mode        TEXT NOT NULL,   -- 'off' | 'blacklist' | 'whitelist'
    PRIMARY KEY (scope_kind, scope_value)
);
CREATE TABLE IF NOT EXISTS filter_rules (
    id          INTEGER PRIMARY KEY,
    scope_kind  TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    list_kind   TEXT NOT NULL,   -- 'block' | 'allow'
    pattern     TEXT NOT NULL,   -- a domain (suffix match) or an IP/CIDR (gateway only)
    created_at  TEXT NOT NULL,
    UNIQUE (scope_kind, scope_value, list_kind, pattern)
);
CREATE INDEX IF NOT EXISTS filter_rules_scope ON filter_rules(scope_kind, scope_value);
"""

MIGRATIONS = [
    "ALTER TABLE devices ADD COLUMN access_state TEXT NOT NULL DEFAULT ''",
]

VendorLookup = Callable[[str], str]

_SELECT_KNOWN = (
    "SELECT d.*, EXISTS(SELECT 1 FROM sessions s WHERE s.mac=d.mac AND s.ended_at IS NULL) AS online "
    "FROM devices d"
)


class Registry:
    """Thread safe wrapper around one SQLite file."""

    def __init__(self, path: Path | str, *, offline_after: int = 3) -> None:
        """offline_after: how many consecutive scans a device may miss before its session closes.

        The CAX80 occasionally drops sleeping devices from one listing and shows them in the next,
        so a single miss should not count as leaving.
        """
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.offline_after = offline_after
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        for statement in MIGRATIONS:
            with contextlib.suppress(sqlite3.OperationalError):  # column already exists
                self._db.execute(statement)

    def close(self) -> None:
        self._db.close()

    # -- scanning --------------------------------------------------------

    def record_scan(
        self,
        devices: list[Device],
        *,
        now: datetime | None = None,
        vendor_lookup: VendorLookup | None = None,
    ) -> ScanDelta:
        """Fold one live device listing into the registry and report what changed."""
        now = now or datetime.now(UTC)
        stamp = _iso(now)
        present = {normalize_mac(d.mac): d for d in devices if d.mac}
        new: list[str] = []
        returned: list[str] = []
        left: list[str] = []

        with self._lock, self._db:
            self._db.execute("BEGIN")
            known = {row["mac"]: row for row in self._db.execute("SELECT * FROM devices")}
            for mac, dev in present.items():
                row = known.get(mac)
                if row is None:
                    vendor = vendor_lookup(mac) if vendor_lookup else ""
                    self._db.execute(
                        """INSERT INTO devices (mac, first_seen, last_seen, last_ip, last_name, last_band,
                                                last_type, last_model, vendor, access_state)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            mac,
                            stamp,
                            stamp,
                            dev.ip,
                            dev.name,
                            dev.band.value,
                            dev.device_type,
                            dev.model,
                            vendor,
                            "allow" if dev.allowed else "block",
                        ),
                    )
                    new.append(mac)
                else:
                    vendor = row["vendor"]
                    if not vendor and vendor_lookup:
                        vendor = vendor_lookup(mac)
                    self._db.execute(
                        """UPDATE devices SET last_seen=?, last_ip=?, last_name=?, last_band=?, last_type=?,
                                              last_model=?, vendor=?, access_state=?, absent_scans=0
                           WHERE mac=?""",
                        (
                            stamp,
                            dev.ip,
                            dev.name,
                            dev.band.value,
                            dev.device_type,
                            dev.model,
                            vendor,
                            "allow" if dev.allowed else "block",
                            mac,
                        ),
                    )
                open_session = self._db.execute(
                    "SELECT id FROM sessions WHERE mac=? AND ended_at IS NULL", (mac,)
                ).fetchone()
                if open_session is None:
                    self._db.execute(
                        "INSERT INTO sessions (mac, started_at, last_seen_at, ip, band) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (mac, stamp, stamp, dev.ip, dev.band.value),
                    )
                    if row is not None:
                        returned.append(mac)
                else:
                    self._db.execute(
                        "UPDATE sessions SET last_seen_at=?, ip=?, band=? WHERE id=?",
                        (stamp, dev.ip, dev.band.value, open_session["id"]),
                    )

            for mac, row in known.items():
                if mac in present:
                    continue
                absent = row["absent_scans"] + 1
                self._db.execute("UPDATE devices SET absent_scans=? WHERE mac=?", (absent, mac))
                if absent >= self.offline_after:
                    closed = self._db.execute(
                        "UPDATE sessions SET ended_at=last_seen_at WHERE mac=? AND ended_at IS NULL", (mac,)
                    ).rowcount
                    if closed:
                        left.append(mac)

            self._db.execute("INSERT INTO scans (scanned_at, present) VALUES (?, ?)", (stamp, len(present)))
            self._db.execute("COMMIT")

        return ScanDelta(
            scanned_at=now,
            present=len(present),
            new=[d for d in (self.get(m) for m in new) if d],
            returned=[d for d in (self.get(m) for m in returned) if d],
            left=[d for d in (self.get(m) for m in left) if d],
        )

    # -- reading ---------------------------------------------------------

    def get(self, mac: str) -> KnownDevice | None:
        mac = normalize_mac(mac)
        with self._lock:
            row = self._db.execute(
                f"""{_SELECT_KNOWN} WHERE mac=?""",
                (mac,),
            ).fetchone()
        return _to_known(row) if row else None

    def all(self) -> list[KnownDevice]:
        with self._lock:
            rows = self._db.execute(f"""{_SELECT_KNOWN} ORDER BY last_seen DESC""").fetchall()
        return [_to_known(r) for r in rows]

    def new_since(self, since: datetime) -> list[KnownDevice]:
        """Devices whose first sighting is after `since`."""
        return [d for d in self.all() if d.first_seen >= since]

    def sessions(self, mac: str, *, limit: int = 20) -> list[PresenceSession]:
        mac = normalize_mac(mac)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions WHERE mac=? ORDER BY started_at DESC LIMIT ?", (mac, limit)
            ).fetchall()
        return [
            PresenceSession(
                mac=r["mac"],
                started_at=_parse(r["started_at"]),
                last_seen_at=_parse(r["last_seen_at"]),
                ended_at=_parse(r["ended_at"]) if r["ended_at"] else None,
                ip=r["ip"],
                band=r["band"],
            )
            for r in rows
        ]

    def resolve(self, query: str) -> list[KnownDevice]:
        """Candidates matching a MAC, nickname, router name or IP. Exact matches only, case insensitive."""
        q = query.strip()
        if not q:
            return []
        mac = normalize_mac(q)
        lowered = q.lower()
        with self._lock:
            rows = self._db.execute(
                f"""{_SELECT_KNOWN}
                   WHERE mac=? OR lower(nickname)=? OR lower(last_name)=? OR last_ip=?
                   ORDER BY last_seen DESC""",
                (mac, lowered, lowered, q),
            ).fetchall()
        return [_to_known(r) for r in rows]

    def by_owner(self, owner: str) -> list[KnownDevice]:
        return [d for d in self.all() if d.owner.lower() == owner.lower()]

    def by_tag(self, tag: str) -> list[KnownDevice]:
        return [d for d in self.all() if tag.lower() in (t.lower() for t in d.tags)]

    # -- writing identity ------------------------------------------------

    def set_identity(
        self,
        mac: str,
        *,
        nickname: str | None = None,
        owner: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
    ) -> KnownDevice:
        """Update the human assigned fields. None leaves a field untouched, "" clears it."""
        mac = normalize_mac(mac)
        updates: dict[str, str] = {}
        if nickname is not None:
            updates["nickname"] = nickname.strip()
        if owner is not None:
            updates["owner"] = owner.strip()
        if tags is not None:
            updates["tags"] = ",".join(sorted({t.strip().lower() for t in tags if t.strip()}))
        if notes is not None:
            updates["notes"] = notes.strip()
        with self._lock:
            exists = self._db.execute("SELECT 1 FROM devices WHERE mac=?", (mac,)).fetchone()
            if not exists:
                raise KeyError(f"{mac} has never been seen on this network")
            if updates:
                assignments = ", ".join(f"{k}=?" for k in updates)
                self._db.execute(f"UPDATE devices SET {assignments} WHERE mac=?", (*updates.values(), mac))
        known = self.get(mac)
        assert known is not None
        return known

    def forget(self, mac: str) -> bool:
        mac = normalize_mac(mac)
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE mac=?", (mac,))
            self._db.execute("DELETE FROM overrides WHERE mac=?", (mac,))
            self._db.execute("DELETE FROM pauses WHERE mac=?", (mac,))
            self._db.execute("DELETE FROM filter_modes WHERE scope_kind='device' AND scope_value=?", (mac,))
            self._db.execute("DELETE FROM filter_rules WHERE scope_kind='device' AND scope_value=?", (mac,))
            return self._db.execute("DELETE FROM devices WHERE mac=?", (mac,)).rowcount > 0

    def merge(self, old_mac: str, new_mac: str) -> KnownDevice:
        """Fold the old device record into the new MAC, then delete the old one.

        Use this when one physical device appears twice, for example after a phone
        stops using a randomized (private) wifi MAC and rejoins with its real one.
        The new MAC is the survivor. Identity fields it is missing (nickname, owner,
        notes) are inherited from the old record, tags are unioned, and the earlier
        `first_seen` is kept. Presence history moves to the new MAC. A block or pause
        on the old MAC moves to the new one only if the new MAC has none of its own.
        """
        old = normalize_mac(old_mac)
        new = normalize_mac(new_mac)
        if old == new:
            raise ValueError("cannot merge a device into itself")
        with self._lock, self._db:
            self._db.execute("BEGIN")
            o = self._db.execute("SELECT * FROM devices WHERE mac=?", (old,)).fetchone()
            n = self._db.execute("SELECT * FROM devices WHERE mac=?", (new,)).fetchone()
            if o is None:
                raise KeyError(f"{old} has never been seen on this network")
            if n is None:
                raise KeyError(f"{new} has never been seen on this network")

            tags = sorted({t for t in (*o["tags"].split(","), *n["tags"].split(",")) if t})
            self._db.execute(
                "UPDATE devices SET nickname=?, owner=?, notes=?, tags=?, first_seen=? WHERE mac=?",
                (
                    n["nickname"] or o["nickname"],
                    n["owner"] or o["owner"],
                    n["notes"] or o["notes"],
                    ",".join(tags),
                    min(o["first_seen"], n["first_seen"]),  # ISO-8601 UTC strings sort chronologically
                    new,
                ),
            )
            self._db.execute("UPDATE sessions SET mac=? WHERE mac=?", (new, old))

            new_has_override = self._db.execute("SELECT 1 FROM overrides WHERE mac=?", (new,)).fetchone()
            if new_has_override:
                self._db.execute("DELETE FROM overrides WHERE mac=?", (old,))
            else:
                self._db.execute("UPDATE overrides SET mac=? WHERE mac=?", (new, old))

            new_has_pause = self._db.execute("SELECT 1 FROM pauses WHERE mac=?", (new,)).fetchone()
            old_pause = self._db.execute("SELECT * FROM pauses WHERE mac=?", (old,)).fetchone()
            if old_pause and not new_has_pause:
                # The old pause IP belonged to the old MAC; enforce against the survivor's current IP.
                self._db.execute(
                    "INSERT INTO pauses (mac, ip, since, reason) VALUES (?, ?, ?, ?)",
                    (new, n["last_ip"], old_pause["since"], old_pause["reason"]),
                )
            self._db.execute("DELETE FROM pauses WHERE mac=?", (old,))

            # Move device-scoped filter mode and rules to the survivor (keep the survivor's own on clash).
            self._db.execute(
                "INSERT OR IGNORE INTO filter_modes (scope_kind, scope_value, mode) "
                "SELECT 'device', ?, mode FROM filter_modes WHERE scope_kind='device' AND scope_value=?",
                (new, old),
            )
            self._db.execute("DELETE FROM filter_modes WHERE scope_kind='device' AND scope_value=?", (old,))
            self._db.execute(
                "INSERT OR IGNORE INTO filter_rules "
                "(scope_kind, scope_value, list_kind, pattern, created_at) "
                "SELECT 'device', ?, list_kind, pattern, created_at FROM filter_rules "
                "WHERE scope_kind='device' AND scope_value=?",
                (new, old),
            )
            self._db.execute("DELETE FROM filter_rules WHERE scope_kind='device' AND scope_value=?", (old,))

            self._db.execute("DELETE FROM devices WHERE mac=?", (old,))
            self._db.execute("COMMIT")
        merged = self.get(new)
        assert merged is not None
        return merged

    # -- groups (tags) ---------------------------------------------------

    def groups(self) -> dict[str, list[KnownDevice]]:
        """Every tag in use, with its members."""
        out: dict[str, list[KnownDevice]] = {}
        for d in self.all():
            for t in d.tags:
                out.setdefault(t, []).append(d)
        return dict(sorted(out.items()))

    def add_tags(self, mac: str, tags: list[str]) -> KnownDevice:
        current = self.get(mac)
        if current is None:
            raise KeyError(f"{normalize_mac(mac)} has never been seen on this network")
        return self.set_identity(mac, tags=[*current.tags, *tags])

    def remove_tags(self, mac: str, tags: list[str]) -> KnownDevice:
        current = self.get(mac)
        if current is None:
            raise KeyError(f"{normalize_mac(mac)} has never been seen on this network")
        drop = {t.strip().lower() for t in tags}
        return self.set_identity(mac, tags=[t for t in current.tags if t not in drop])

    # -- access state, rules, overrides ------------------------------------

    def set_access_state(self, mac: str, allow: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE devices SET access_state=? WHERE mac=?",
                ("allow" if allow else "block", normalize_mac(mac)),
            )

    def add_rule(self, rule: Rule) -> Rule:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO rules (name, kind, target, start, end, days, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rule.name,
                    rule.kind,
                    rule.target,
                    rule.start,
                    rule.end,
                    ",".join(map(str, rule.days)),
                    rule.enabled,
                ),
            )
            rule_id = cur.lastrowid
        assert rule_id is not None
        return rule.model_copy(update={"id": rule_id})

    def rules(self) -> list[Rule]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM rules ORDER BY id").fetchall()
        return [
            Rule(
                id=r["id"],
                name=r["name"],
                kind=r["kind"],
                target=r["target"],
                start=r["start"],
                end=r["end"],
                days=[int(d) for d in r["days"].split(",") if d],
                enabled=bool(r["enabled"]),
            )
            for r in rows
        ]

    def remove_rule(self, rule_id: int) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        with self._lock:
            return self._db.execute("UPDATE rules SET enabled=? WHERE id=?", (enabled, rule_id)).rowcount > 0

    def set_override(self, mac: str, *, allow: bool, until: datetime | None, reason: str = "") -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO overrides (mac, allow, set_at, until, reason) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET allow=excluded.allow, set_at=excluded.set_at,
                                                  until=excluded.until, reason=excluded.reason""",
                (normalize_mac(mac), allow, _iso(datetime.now(UTC)), _iso(until) if until else None, reason),
            )

    def clear_override(self, mac: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM overrides WHERE mac=?", (normalize_mac(mac),)).rowcount > 0

    def overrides(self) -> dict[str, AccessOverride]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM overrides").fetchall()
        return {
            r["mac"]: AccessOverride(
                mac=r["mac"],
                allow=bool(r["allow"]),
                set_at=_parse(r["set_at"]),
                until=_parse(r["until"]) if r["until"] else None,
                reason=r["reason"],
            )
            for r in rows
        }

    # -- pauses (Eclipse) --------------------------------------------------

    def set_pause(self, mac: str, ip: str, reason: str = "") -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO pauses (mac, ip, since, reason) VALUES (?, ?, ?, ?)
                   ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip, reason=excluded.reason""",
                (normalize_mac(mac), ip, _iso(datetime.now(UTC)), reason),
            )

    def update_pause_ip(self, mac: str, ip: str) -> None:
        with self._lock:
            self._db.execute("UPDATE pauses SET ip=? WHERE mac=?", (ip, normalize_mac(mac)))

    def clear_pause(self, mac: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM pauses WHERE mac=?", (normalize_mac(mac),)).rowcount > 0

    def clear_all_pauses(self) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM pauses").rowcount

    def pauses(self) -> list[PauseTarget]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM pauses ORDER BY since").fetchall()
            names = {
                r["mac"]: (r["nickname"] or r["last_name"] or r["last_model"] or r["vendor"])
                for r in self._db.execute("SELECT * FROM devices")
            }
        return [
            PauseTarget(
                mac=r["mac"],
                ip=r["ip"],
                since=_parse(r["since"]),
                reason=r["reason"],
                display_name=names.get(r["mac"], ""),
            )
            for r in rows
        ]

    def last_scan(self) -> datetime | None:
        with self._lock:
            row = self._db.execute("SELECT scanned_at FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return _parse(row["scanned_at"]) if row else None

    # -- DNS filter: per-scope mode and allow/deny rules ------------------

    def set_filter_mode(self, scope_kind: str, scope_value: str, mode: str) -> None:
        """Set the filtering mode ('off', 'blacklist', 'whitelist') for a scope. 'off' clears it."""
        with self._lock:
            if mode == "off":
                self._db.execute(
                    "DELETE FROM filter_modes WHERE scope_kind=? AND scope_value=?",
                    (scope_kind, scope_value),
                )
            else:
                self._db.execute(
                    """INSERT INTO filter_modes (scope_kind, scope_value, mode) VALUES (?, ?, ?)
                       ON CONFLICT(scope_kind, scope_value) DO UPDATE SET mode=excluded.mode""",
                    (scope_kind, scope_value, mode),
                )

    def get_filter_mode(self, scope_kind: str, scope_value: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT mode FROM filter_modes WHERE scope_kind=? AND scope_value=?",
                (scope_kind, scope_value),
            ).fetchone()
        return row["mode"] if row else "off"

    def filter_modes(self) -> list[tuple[str, str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT scope_kind, scope_value, mode FROM filter_modes ORDER BY scope_kind, scope_value"
            ).fetchall()
        return [(r["scope_kind"], r["scope_value"], r["mode"]) for r in rows]

    def add_filter_rule(self, scope_kind: str, scope_value: str, list_kind: str, pattern: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO filter_rules (scope_kind, scope_value, list_kind, pattern, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(scope_kind, scope_value, list_kind, pattern) DO NOTHING""",
                (scope_kind, scope_value, list_kind, pattern, _iso(datetime.now(UTC))),
            )

    def remove_filter_rule(self, scope_kind: str, scope_value: str, list_kind: str, pattern: str) -> bool:
        with self._lock:
            return (
                self._db.execute(
                    """DELETE FROM filter_rules
                       WHERE scope_kind=? AND scope_value=? AND list_kind=? AND pattern=?""",
                    (scope_kind, scope_value, list_kind, pattern),
                ).rowcount
                > 0
            )

    def filter_rules(
        self, scope_kind: str | None = None, scope_value: str | None = None
    ) -> list[tuple[str, str, str, str]]:
        """(scope_kind, scope_value, list_kind, pattern), optionally narrowed to one scope."""
        query = "SELECT scope_kind, scope_value, list_kind, pattern FROM filter_rules"
        params: tuple[str, ...] = ()
        if scope_kind is not None:
            query += " WHERE scope_kind=? AND scope_value=?"
            params = (scope_kind, scope_value or "")
        query += " ORDER BY scope_kind, scope_value, list_kind, pattern"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [(r["scope_kind"], r["scope_value"], r["list_kind"], r["pattern"]) for r in rows]


# -- helpers -------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_known(row: sqlite3.Row) -> KnownDevice:
    return KnownDevice(
        mac=row["mac"],
        first_seen=_parse(row["first_seen"]),
        last_seen=_parse(row["last_seen"]),
        last_ip=row["last_ip"],
        last_name=row["last_name"],
        last_band=row["last_band"],
        last_type=row["last_type"],
        last_model=row["last_model"],
        vendor=row["vendor"],
        nickname=row["nickname"],
        owner=row["owner"],
        tags=[t for t in row["tags"].split(",") if t],
        notes=row["notes"],
        online=bool(row["online"]),
        randomized_mac=is_randomized(row["mac"]),
        access_state=row["access_state"],
    )


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """'7d', '12h', '30m', or an ISO date/datetime -> aware datetime."""
    now = now or datetime.now(UTC)
    t = text.strip().lower()
    units = {
        "d": timedelta(days=1),
        "h": timedelta(hours=1),
        "m": timedelta(minutes=1),
        "w": timedelta(weeks=1),
    }
    if t and t[-1] in units and t[:-1].isdigit():
        return now - int(t[:-1]) * units[t[-1]]
    dt = datetime.fromisoformat(text.strip())
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
