"""Tests for schedule evaluation, incl. DST handling."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from custom_components.thermostat_schedule.const import DAYS
from custom_components.thermostat_schedule.models import default_week, parse_day
from custom_components.thermostat_schedule.schedule import (
    next_change,
    next_switch_time,
    setpoint_at,
)

BERLIN = ZoneInfo("Europe/Berlin")


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=BERLIN)


def test_setpoint_by_time_of_day() -> None:
    week = default_week()
    assert setpoint_at(week, local(2026, 10, 26, 3), BERLIN) == 17.0
    assert setpoint_at(week, local(2026, 10, 26, 6), BERLIN) == 21.0
    assert setpoint_at(week, local(2026, 10, 26, 21, 59), BERLIN) == 21.0
    assert setpoint_at(week, local(2026, 10, 26, 22), BERLIN) == 17.0


def test_weekday_specific() -> None:
    week = default_week()
    week["sat"] = parse_day("00:00 17\n09:00 22")
    assert setpoint_at(week, local(2026, 10, 24, 7), BERLIN) == 17.0  # Saturday
    assert setpoint_at(week, local(2026, 10, 24, 10), BERLIN) == 22.0
    assert setpoint_at(week, local(2026, 10, 26, 7), BERLIN) == 21.0  # Monday


def test_next_change_crosses_midnight_and_skips_equal_temps() -> None:
    week = default_week()
    now = local(2026, 10, 26, 23)
    when, temp = next_change(week, now, BERLIN)
    # 17 °C at night; midnight point is also 17 -> next real change is 06:00 Tuesday
    assert (when, temp) == (local(2026, 10, 27, 6), 21.0)
    assert next_switch_time(week, now, BERLIN) == local(2026, 10, 27, 0)


def test_timezone_aware_input_in_utc() -> None:
    week = default_week()
    now = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)  # 06:00 CET
    assert setpoint_at(week, now, BERLIN) == 21.0


def test_dst_spring_forward() -> None:
    # 2026-03-29: 02:00 -> 03:00 in Europe/Berlin; a 02:30 switch point does not exist.
    week = {d: parse_day("00:00 17\n02:30 20\n06:00 21") for d in DAYS}
    before = local(2026, 3, 29, 1, 59)
    assert setpoint_at(week, before, BERLIN) == 17.0
    after = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # 03:30 local
    assert setpoint_at(week, after, BERLIN) == 20.0


def test_dst_fall_back_next_change_is_monotonic() -> None:
    # 2026-10-25: 03:00 -> 02:00 (the 02:xx hour occurs twice).
    week = {d: parse_day("00:00 17\n06:00 21") for d in DAYS}
    now = datetime(2026, 10, 25, 2, 30, tzinfo=UTC)  # 03:30 CET, after fold
    when, temp = next_change(week, now, BERLIN)
    assert when > now and temp == 21.0
