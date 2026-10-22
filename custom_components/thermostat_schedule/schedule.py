"""Pure schedule evaluation (no HA imports): current setpoint and next change."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from .const import DAYS
from .models import DaySchedule, WeekSchedule

_LOOKAROUND_DAYS = (-1, 0, 1, 2)


def _timeline(week: WeekSchedule, now: datetime, tz: tzinfo) -> list[tuple[datetime, float]]:
    """Return aware (start, temp) pairs for yesterday through two days ahead.

    Building aware datetimes from local wall-clock dates keeps DST transitions
    correct: wall times that do not exist or occur twice still map to valid instants.
    """
    today = now.astimezone(tz).date()
    result: list[tuple[datetime, float]] = []
    for offset in _LOOKAROUND_DAYS:
        day = today + timedelta(days=offset)
        points: DaySchedule = week[DAYS[day.weekday()]]
        for point in points:
            start = datetime(
                day.year, day.month, day.day, point.minute // 60, point.minute % 60, tzinfo=tz
            )
            result.append((start, point.temp))
    result.sort(key=lambda item: item[0])
    return result


def setpoint_at(week: WeekSchedule, now: datetime, tz: tzinfo) -> float:
    """Return the scheduled temperature at ``now``."""
    current = None
    for start, temp in _timeline(week, now, tz):
        if start <= now:
            current = temp
        else:
            break
    assert current is not None  # every day starts at 00:00, so yesterday always matches
    return current


def next_change(week: WeekSchedule, now: datetime, tz: tzinfo) -> tuple[datetime, float] | None:
    """Return the next switch point strictly after ``now`` whose temperature differs.

    Consecutive points with the same temperature are not reported as a change.
    """
    timeline = _timeline(week, now, tz)
    current = setpoint_at(week, now, tz)
    for start, temp in timeline:
        if start > now and temp != current:
            return start, temp
    return None


def next_switch_time(week: WeekSchedule, now: datetime, tz: tzinfo) -> datetime | None:
    """Return the next switch point strictly after ``now``, regardless of temperature."""
    for start, _temp in _timeline(week, now, tz):
        if start > now:
            return start
    return None
