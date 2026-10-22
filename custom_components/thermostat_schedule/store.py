"""Persistent per-hub state: default schedules, room overrides, switches, pins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DAYS, STORAGE_KEY, STORAGE_VERSION
from .models import (
    DaySchedule,
    ScheduleError,
    WeekSchedule,
    day_from_json,
    day_to_json,
    default_week,
    week_from_json,
    week_to_json,
)

SAVE_DELAY = 2.0


@dataclass
class RoomState:
    """Persistent state of one room.

    ``overrides`` only holds the days a room customizes (profile -> day -> schedule);
    every other day follows the hub's default schedule.
    """

    auto: bool = True
    pin: str | None = None
    overrides: dict[str, dict[str, DaySchedule]] = field(default_factory=dict)


class HubStore:
    """Wraps a ``Store`` and keeps parsed state in memory."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY.format(entry_id=entry_id)
        )
        self.base_profile: str | None = None
        self.enabled: bool = True
        self.defaults: dict[str, WeekSchedule] = {}
        self.rooms: dict[str, RoomState] = {}

    async def async_load(self) -> None:
        """Load state from disk (missing file -> empty defaults)."""
        data = await self._store.async_load() or {}
        self.base_profile = data.get("base_profile")
        self.enabled = bool(data.get("enabled", True))
        if "defaults" not in data and data.get("rooms"):
            data = _migrate_full_room_schedules(data)
        self.defaults = {
            profile: week_from_json(week) for profile, week in (data.get("defaults") or {}).items()
        }
        self.rooms = {}
        for room_id, raw in (data.get("rooms") or {}).items():
            overrides: dict[str, dict[str, DaySchedule]] = {}
            for profile, days in (raw.get("overrides") or {}).items():
                for day, points in days.items():
                    if day not in DAYS:
                        continue
                    try:
                        overrides.setdefault(profile, {})[day] = day_from_json(points)
                    except ScheduleError:
                        continue
            self.rooms[room_id] = RoomState(
                auto=bool(raw.get("auto", True)), pin=raw.get("pin"), overrides=overrides
            )

    def _as_json(self) -> dict[str, Any]:
        return {
            "base_profile": self.base_profile,
            "enabled": self.enabled,
            "defaults": {p: week_to_json(w) for p, w in self.defaults.items()},
            "rooms": {
                room_id: {
                    "auto": room.auto,
                    "pin": room.pin,
                    "overrides": {
                        p: {d: day_to_json(pts) for d, pts in days.items()}
                        for p, days in room.overrides.items()
                    },
                }
                for room_id, room in self.rooms.items()
            },
        }

    def async_schedule_save(self) -> None:
        """Save soon (debounced)."""
        self._store.async_delay_save(self._as_json, SAVE_DELAY)

    async def async_save(self) -> None:
        """Save immediately."""
        await self._store.async_save(self._as_json())

    def sync_with_config(
        self, *, profiles: list[str], room_ids: list[str], default_base: str
    ) -> None:
        """Reconcile stored state with the current profiles and rooms.

        Creates missing default schedules, drops rooms/profiles that no longer exist and
        repairs an invalid base profile and pins.
        """
        self.defaults = {p: self.defaults.get(p) or default_week() for p in profiles}
        self.rooms = {rid: self.rooms.get(rid, RoomState()) for rid in room_ids}
        for room in self.rooms.values():
            room.overrides = {p: d for p, d in room.overrides.items() if p in profiles and d}
            if room.pin not in (None, *profiles):
                room.pin = None
        if self.base_profile not in profiles:
            self.base_profile = default_base if default_base in profiles else profiles[0]


def _migrate_full_room_schedules(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the first storage layout (full schedules per room) to defaults + overrides.

    The first room's schedules become the hub defaults; every other day of every room
    that differs from them becomes a room override.
    """
    rooms = data.get("rooms") or {}
    first = next((r["schedules"] for r in rooms.values() if r.get("schedules")), {})
    defaults = {p: week_from_json(w) for p, w in first.items()}
    migrated_rooms: dict[str, Any] = {}
    for room_id, raw in rooms.items():
        overrides: dict[str, dict[str, Any]] = {}
        for profile, week_json in (raw.get("schedules") or {}).items():
            week = week_from_json(week_json)
            base = defaults.get(profile) or default_week()
            for day in DAYS:
                if week[day] != base[day]:
                    overrides.setdefault(profile, {})[day] = day_to_json(week[day])
        migrated_rooms[room_id] = {
            "auto": raw.get("auto", True),
            "pin": raw.get("pin"),
            "overrides": overrides,
        }
    return {
        "base_profile": data.get("base_profile"),
        "enabled": data.get("enabled", True),
        "defaults": {p: week_to_json(w) for p, w in defaults.items()},
        "rooms": migrated_rooms,
    }
