"""Rule evaluation: day parsing, active windows across midnight, boundaries, desired state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from curfew.models import AccessOverride, KnownDevice, Rule
from curfew.scheduler import (
    desired_state,
    format_days,
    is_active,
    next_boundary,
    parse_days,
    rule_applies_to,
)

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def bedtime(**kw: object) -> Rule:
    base = {"name": "bedtime", "kind": "group", "target": "kids", "start": "21:00", "end": "07:00"}
    return Rule(**{**base, **kw})  # type: ignore[arg-type]


def kid(tags: list[str] = ["kids"], owner: str = "Sam") -> KnownDevice:  # noqa: B006
    return KnownDevice(mac="AA:BB:CC:00:00:01", first_seen=T0, last_seen=T0, tags=tags, owner=owner)


def test_parse_and_format_days() -> None:
    assert parse_days("daily") == list(range(7))
    assert parse_days("weekdays") == [0, 1, 2, 3, 4]
    assert parse_days("sat,sun") == [5, 6]
    assert parse_days("fri-mon") == [0, 4, 5, 6]
    assert parse_days("Wednesday") == [2]
    assert format_days([0, 1, 2, 3, 4]) == "weekdays"
    assert format_days([5, 6]) == "weekends"
    assert format_days([1, 3]) == "tue,thu"
    with pytest.raises(ValueError):
        parse_days("someday")


def test_active_window_crossing_midnight() -> None:
    rule = bedtime()  # every day 21:00 -> 07:00
    assert is_active(rule, datetime(2026, 9, 12, 21, 0, tzinfo=NY))
    assert is_active(rule, datetime(2026, 9, 13, 3, 0, tzinfo=NY))
    assert is_active(rule, datetime(2026, 9, 13, 6, 59, tzinfo=NY))
    assert not is_active(rule, datetime(2026, 9, 13, 7, 0, tzinfo=NY))
    assert not is_active(rule, datetime(2026, 9, 12, 20, 59, tzinfo=NY))
    assert not is_active(bedtime(enabled=False), datetime(2026, 9, 12, 22, 0, tzinfo=NY))


def test_active_window_school_nights_only() -> None:
    rule = bedtime(days=[6, 0, 1, 2, 3])  # Sun-Thu nights
    friday_night = datetime(2026, 9, 11, 22, 0, tzinfo=NY)  # 2026-09-11 is a Friday
    saturday_early = datetime(2026, 9, 12, 2, 0, tzinfo=NY)
    sunday_night = datetime(2026, 9, 13, 22, 0, tzinfo=NY)
    monday_early = datetime(2026, 9, 14, 6, 0, tzinfo=NY)
    assert not is_active(rule, friday_night)
    assert not is_active(rule, saturday_early)  # Friday is not a listed day
    assert is_active(rule, sunday_night)
    assert is_active(rule, monday_early)  # carried over from Sunday


def test_same_day_window_and_all_day() -> None:
    homework = bedtime(start="16:00", end="18:00")
    assert is_active(homework, datetime(2026, 9, 12, 17, 0, tzinfo=NY))
    assert not is_active(homework, datetime(2026, 9, 12, 18, 0, tzinfo=NY))
    all_day = bedtime(start="00:00", end="00:00", days=[5])
    assert is_active(all_day, datetime(2026, 9, 12, 12, 0, tzinfo=NY))  # Saturday
    assert not is_active(all_day, datetime(2026, 9, 13, 12, 0, tzinfo=NY))


def test_next_boundary() -> None:
    rule = bedtime()
    evening = datetime(2026, 9, 12, 19, 0, tzinfo=NY)
    assert next_boundary([rule], evening) == datetime(2026, 9, 12, 21, 0, tzinfo=NY)
    night = datetime(2026, 9, 12, 23, 0, tzinfo=NY)
    assert next_boundary([rule], night) == datetime(2026, 9, 13, 7, 0, tzinfo=NY)
    assert next_boundary([], evening) is None
    assert next_boundary([bedtime(enabled=False)], evening) is None


def test_rule_targets() -> None:
    assert rule_applies_to(bedtime(), kid())
    assert not rule_applies_to(bedtime(), kid(tags=["iot"]))
    assert rule_applies_to(bedtime(kind="owner", target="sam"), kid())
    assert rule_applies_to(bedtime(kind="device", target="aa:bb:cc:00:00:01"), kid())
    assert not rule_applies_to(bedtime(kind="device", target="aa:bb:cc:00:00:02"), kid())


def test_desired_state_precedence() -> None:
    rule = bedtime()
    night = datetime(2026, 9, 13, 3, 0, tzinfo=NY).astimezone(UTC)
    day = datetime(2026, 9, 13, 12, 0, tzinfo=NY).astimezone(UTC)
    assert desired_state(kid(), [rule], None, night) is False
    assert desired_state(kid(), [rule], None, day) is True
    assert desired_state(kid(tags=[]), [rule], None, night) is None  # unmanaged
    manual_on = AccessOverride(mac="x", allow=True, set_at=night, until=night + timedelta(hours=1))
    assert desired_state(kid(), [rule], manual_on, night) is True
    assert desired_state(kid(), [rule], manual_on, night + timedelta(hours=2)) is False  # expired
    manual_off = AccessOverride(mac="x", allow=False, set_at=day, until=None)
    assert desired_state(kid(tags=[]), [], manual_off, day) is False  # sticky, no rules
