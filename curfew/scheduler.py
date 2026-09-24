"""Time based access rules and the logic that turns rules plus manual overrides into a desired state.

Rules are evaluated in local wall clock time. A rule with start 21:00 and end 07:00 is active from
21:00 on each listed day until 07:00 the next morning.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from curfew.models import AccessOverride, Band, KnownDevice, Rule

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

GUEST_KIND = "guest"
GUEST_TARGETS = {"both": (Band.GHZ_2_4, Band.GHZ_5), "2.4": (Band.GHZ_2_4,), "5": (Band.GHZ_5,)}


def guest_bands(rule: Rule) -> tuple[Band, ...]:
    """Bands a guest-network rule covers. Empty for any rule that is not a guest rule."""
    if rule.kind != GUEST_KIND:
        return ()
    return GUEST_TARGETS.get(rule.target, ())


def desired_guest(rules: list[Rule], band: Band, now_local: datetime) -> bool | None:
    """True = guest network on, False = off, None = no guest rule covers this band.

    A guest rule's window is when the guest network is off, like a bedtime window blocks devices.
    """
    mine = [r for r in rules if r.enabled and band in guest_bands(r)]
    if not mine:
        return None
    return not any(is_active(r, now_local) for r in mine)


def parse_days(text: str) -> list[int]:
    """'mon-fri', 'sat,sun', 'daily', 'weekdays', 'weekends' -> weekday numbers (0 = Monday)."""
    t = text.strip().lower()
    if t in {"", "daily", "all", "everyday", "every day", "mon-sun"}:
        return list(range(7))
    if t in {"weekdays", "school"}:
        return [0, 1, 2, 3, 4]
    if t in {"weekends", "weekend"}:
        return [5, 6]
    days: set[int] = set()
    for part in t.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (DAY_NAMES.index(p.strip()[:3]) for p in part.split("-", 1))
            i = a
            while True:
                days.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        elif part:
            days.add(DAY_NAMES.index(part[:3]))
    return sorted(days)


def format_days(days: list[int]) -> str:
    if sorted(days) == list(range(7)):
        return "daily"
    if sorted(days) == [0, 1, 2, 3, 4]:
        return "weekdays"
    if sorted(days) == [5, 6]:
        return "weekends"
    return ",".join(DAY_NAMES[d] for d in sorted(days))


def parse_hhmm(text: str) -> time:
    hours, minutes = text.strip().split(":")
    return time(int(hours), int(minutes))


def rule_applies_to(rule: Rule, device: KnownDevice) -> bool:
    if rule.kind == "group":
        return rule.target.lower() in (t.lower() for t in device.tags)
    if rule.kind == "owner":
        return rule.target.lower() == device.owner.lower()
    if rule.kind == "device":
        return rule.target.upper() == device.mac.upper()
    return False


def is_active(rule: Rule, now_local: datetime) -> bool:
    if not rule.enabled:
        return False
    start, end = parse_hhmm(rule.start), parse_hhmm(rule.end)
    today = now_local.weekday()
    yesterday = (today - 1) % 7
    t = now_local.time().replace(second=0, microsecond=0)
    if start < end:
        return today in rule.days and start <= t < end
    if start == end:
        return today in rule.days  # all day
    # crosses midnight
    return (today in rule.days and t >= start) or (yesterday in rule.days and t < end)


def next_boundary(rules: list[Rule], now_local: datetime) -> datetime | None:
    """The next moment any of the rules starts or ends, within a week. None when there are no rules."""
    candidates: list[datetime] = []
    for rule in rules:
        if not rule.enabled:
            continue
        for offset in range(8):
            day = (now_local + timedelta(days=offset)).date()
            for hhmm in (rule.start, rule.end):
                moment = datetime.combine(day, parse_hhmm(hhmm), tzinfo=now_local.tzinfo)
                if moment > now_local and is_active(rule, moment) != is_active(
                    rule, moment - timedelta(minutes=1)
                ):
                    candidates.append(moment)
    return min(candidates) if candidates else None


def desired_state(
    device: KnownDevice,
    rules: list[Rule],
    override: AccessOverride | None,
    now: datetime,
) -> bool | None:
    """True = allow, False = block, None = nothing wants to manage this device."""
    if override is not None and (override.until is None or override.until > now):
        return override.allow
    mine = [r for r in rules if rule_applies_to(r, device)]
    if not mine:
        return None
    now_local = now.astimezone()
    return not any(is_active(r, now_local) for r in mine)


def override_expired(override: AccessOverride, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return override.until is not None and override.until <= now
