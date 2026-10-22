"""Pure schedule data model: parsing, formatting and validation (no HA imports)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from .const import (
    DAYS,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    MAX_POINTS_PER_DAY,
    MAX_TEMP,
    MIN_TEMP,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESETS,
)


class ScheduleError(ValueError):
    """Invalid schedule input. ``code`` doubles as a translation key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# A switch point's temperature: degrees Celsius or the name of a room preset ("eco"/"comfort").
Temp = float | str

DEFAULT_PRESETS: dict[str, float] = {
    PRESET_ECO: DEFAULT_ECO_TEMP,
    PRESET_COMFORT: DEFAULT_COMFORT_TEMP,
}


class SwitchPoint(NamedTuple):
    """A temperature that starts at ``minute`` (minutes after midnight)."""

    minute: int
    temp: Temp


DaySchedule = list[SwitchPoint]
# One profile's full week: day key -> switch points.
WeekSchedule = dict[str, DaySchedule]

_LINE_RE = re.compile(
    r"^\s*(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?:=|-|;|\s)\s*(?P<t>\d+(?:[.,]\d+)?|eco|comfort)\s*(?:°\s*C?|C)?\s*$",
    re.IGNORECASE,
)

DEFAULT_DAY: DaySchedule = [
    SwitchPoint(0, PRESET_ECO),
    SwitchPoint(8 * 60, PRESET_COMFORT),
    SwitchPoint(22 * 60 + 30, PRESET_ECO),
]


def default_week() -> WeekSchedule:
    """Return a fresh default week (eco, comfort from 08:00 to 22:30)."""
    return {day: list(DEFAULT_DAY) for day in DAYS}


def format_minute(minute: int) -> str:
    """Format minutes after midnight as HH:MM."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def format_temp(temp: Temp) -> str:
    """Format a temperature compactly (21.0 -> '21', 21.5 -> '21.5', presets by name)."""
    return temp if isinstance(temp, str) else f"{temp:g}"


def resolve_temp(temp: Temp, presets: Mapping[str, float] | None = None) -> float:
    """Resolve a switch point temperature to degrees using a room's presets."""
    if isinstance(temp, str):
        return float((presets or DEFAULT_PRESETS).get(temp, DEFAULT_PRESETS[temp]))
    return temp


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
        if isinstance(point.temp, str):
            if point.temp not in PRESETS:
                raise ScheduleError("invalid_format")
        elif not MIN_TEMP <= point.temp <= MAX_TEMP:
            raise ScheduleError("temp_range")
    return points


def parse_day(text: str) -> DaySchedule:
    """Parse editor text (``HH:MM temperature`` per line or comma separated) into a validated day.

    A temperature is a number (dot or comma decimal) or a preset name (``eco``/``comfort``).

    If the first entry is not 00:00, a 00:00 entry is prepended that carries the
    temperature of the last entry (the night temperature wraps around midnight).
    """
    points: DaySchedule = []
    for raw in re.split(r"\n|,\s*(?=\d{1,2}:\d{2})", text):
        if not raw.strip():
            continue
        match = _LINE_RE.match(raw)
        if not match:
            raise ScheduleError("invalid_format")
        hour, minute = int(match["h"]), int(match["m"])
        if hour > 23 or minute > 59:
            raise ScheduleError("invalid_time")
        token = match["t"].lower()
        temp: Temp = token if token in PRESETS else float(token.replace(",", "."))
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
            raw = item["temp"]
            temp: Temp = raw.lower() if isinstance(raw, str) else float(raw)
            points.append(SwitchPoint(int(hour) * 60 + int(minute), temp))
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
