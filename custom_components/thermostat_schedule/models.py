"""Pure schedule data model: parsing, formatting and validation (no HA imports)."""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from .const import DAYS, MAX_POINTS_PER_DAY, MAX_TEMP, MIN_TEMP


class ScheduleError(ValueError):
    """Invalid schedule input. ``code`` doubles as a translation key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SwitchPoint(NamedTuple):
    """A temperature that starts at ``minute`` (minutes after midnight)."""

    minute: int
    temp: float


DaySchedule = list[SwitchPoint]
# One profile's full week: day key -> switch points.
WeekSchedule = dict[str, DaySchedule]

_LINE_RE = re.compile(
    r"^\s*(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?:=|-|;|\s)\s*(?P<t>\d+(?:[.,]\d+)?)\s*(?:°\s*C?|C)?\s*$",
    re.IGNORECASE,
)

DEFAULT_DAY: DaySchedule = [
    SwitchPoint(0, 17.0),
    SwitchPoint(6 * 60, 21.0),
    SwitchPoint(22 * 60, 17.0),
]


def default_week() -> WeekSchedule:
    """Return a fresh default week (17 °C night, 21 °C from 06:00 to 22:00)."""
    return {day: list(DEFAULT_DAY) for day in DAYS}


def format_minute(minute: int) -> str:
    """Format minutes after midnight as HH:MM."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def format_temp(temp: float) -> str:
    """Format a temperature compactly (21.0 -> '21', 21.5 -> '21.5')."""
    return f"{temp:g}"


def format_day(points: DaySchedule) -> str:
    """Format a day as the multi-line text used by the editor."""
    return "\n".join(f"{format_minute(p.minute)} {format_temp(p.temp)}" for p in points)


def validate_day(points: DaySchedule) -> DaySchedule:
    """Validate a day schedule, raising ScheduleError. Returns the points unchanged."""
    if not points:
        raise ScheduleError("empty_day")
    if len(points) > MAX_POINTS_PER_DAY:
        raise ScheduleError("too_many_points")
    if points[0].minute != 0:
        raise ScheduleError("first_point_midnight")
    last = -1
    for point in points:
        if not 0 <= point.minute < 24 * 60:
            raise ScheduleError("invalid_time")
        if point.minute <= last:
            raise ScheduleError("not_increasing")
        last = point.minute
        if not MIN_TEMP <= point.temp <= MAX_TEMP:
            raise ScheduleError("temp_range")
    return points


def parse_day(text: str) -> DaySchedule:
    """Parse editor text (one ``HH:MM temperature`` per line) into a validated day.

    If the first entry is not 00:00, a 00:00 entry is prepended that carries the
    temperature of the last entry (the night temperature wraps around midnight).
    """
    points: DaySchedule = []
    for raw in text.replace(",\n", "\n").splitlines():
        if not raw.strip():
            continue
        match = _LINE_RE.match(raw)
        if not match:
            raise ScheduleError("invalid_format")
        hour, minute = int(match["h"]), int(match["m"])
        if hour > 23 or minute > 59:
            raise ScheduleError("invalid_time")
        temp = float(match["t"].replace(",", "."))
        points.append(SwitchPoint(hour * 60 + minute, temp))
    if not points:
        raise ScheduleError("empty_day")
    points.sort(key=lambda p: p.minute)
    if points[0].minute != 0:
        points.insert(0, SwitchPoint(0, points[-1].temp))
    return validate_day(points)


def day_to_json(points: DaySchedule) -> list[dict[str, Any]]:
    """Serialize a day for storage."""
    return [{"start": format_minute(p.minute), "temp": p.temp} for p in points]


def day_from_json(data: list[dict[str, Any]]) -> DaySchedule:
    """Deserialize and validate a stored day."""
    points: DaySchedule = []
    for item in data:
        try:
            hour, minute = str(item["start"]).split(":")
            points.append(SwitchPoint(int(hour) * 60 + int(minute), float(item["temp"])))
        except (KeyError, ValueError, TypeError) as err:
            raise ScheduleError("invalid_format") from err
    return validate_day(points)


def week_to_json(week: WeekSchedule) -> dict[str, list[dict[str, Any]]]:
    """Serialize a week for storage."""
    return {day: day_to_json(week[day]) for day in DAYS}


def week_from_json(data: dict[str, Any]) -> WeekSchedule:
    """Deserialize a week; missing or invalid days fall back to the default day."""
    week: WeekSchedule = {}
    for day in DAYS:
        try:
            week[day] = day_from_json(data[day])
        except KeyError, ScheduleError, TypeError:
            week[day] = list(DEFAULT_DAY)
    return week
