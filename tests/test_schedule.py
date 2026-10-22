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
    assert setpoint_at(week, local(2026, 10, 26, 3), BERLIN) == 19.0
    assert setpoint_at(week, local(2026, 10, 26, 7, 59), BERLIN) == 19.0
    assert setpoint_at(week, local(2026, 10, 26, 8), BERLIN) == 21.5
    assert setpoint_at(week, local(2026, 10, 26, 22, 29), BERLIN) == 21.5
    assert setpoint_at(week, local(2026, 10, 26, 22, 30), BERLIN) == 19.0


def test_weekday_specific() -> None:
    week = default_week()
    week["sat"] = parse_day("00:00 17\n09:00 22")
    assert setpoint_at(week, local(2026, 10, 24, 7), BERLIN) == 17.0  # Saturday
    assert setpoint_at(week, local(2026, 10, 24, 10), BERLIN) == 22.0
    assert setpoint_at(week, local(2026, 10, 26, 7), BERLIN) == 19.0  # Monday


def test_next_change_crosses_midnight_and_skips_equal_temps() -> None:
    week = default_week()
    now = local(2026, 10, 26, 23)
    when, temp = next_change(week, now, BERLIN)
    # 19 °C at night; midnight point is also 19 -> next real change is 08:00 Tuesday
    assert (when, temp) == (local(2026, 10, 27, 8), 21.5)
    assert next_switch_time(week, now, BERLIN) == local(2026, 10, 27, 0)


def test_timezone_aware_input_in_utc() -> None:
    week = default_week()
    now = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)  # 08:00 CET
    assert setpoint_at(week, now, BERLIN) == 21.5


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


def test_presets_are_resolved_per_room() -> None:
    week = {d: parse_day("00:00 eco, 08:00 comfort, 19:00 23, 22:30 eco") for d in DAYS}
    cool = {"eco": 16.0, "comfort": 20.0}
    warm = {"eco": 19.0, "comfort": 22.0}
    morning = local(2026, 10, 26, 9)
    assert setpoint_at(week, morning, BERLIN, cool) == 20.0
    assert setpoint_at(week, morning, BERLIN, warm) == 22.0
    assert setpoint_at(week, local(2026, 10, 26, 20), BERLIN, cool) == 23.0
    assert setpoint_at(week, local(2026, 10, 26, 23), BERLIN, cool) == 16.0
    assert next_change(week, morning, BERLIN, cool) == (local(2026, 10, 26, 19), 23.0)
