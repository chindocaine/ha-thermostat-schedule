"""Persistent per-hub state: schedules, base profile, switches, pins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DAYS, STORAGE_KEY, STORAGE_VERSION
from .models import WeekSchedule, default_week, week_from_json, week_to_json

SAVE_DELAY = 2.0


@dataclass
class RoomState:
    """Persistent state of one room."""

    auto: bool = True
    pin: str | None = None
    schedules: dict[str, WeekSchedule] = field(default_factory=dict)


class HubStore:
    """Wraps a ``Store`` and keeps parsed state in memory."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY.format(entry_id=entry_id)
        )
        self.base_profile: str | None = None
        self.enabled: bool = True
        self.rooms: dict[str, RoomState] = {}

    async def async_load(self) -> None:
        """Load state from disk (missing file -> empty defaults)."""
        data = await self._store.async_load() or {}
        self.base_profile = data.get("base_profile")
        self.enabled = bool(data.get("enabled", True))
        self.rooms = {}
        for room_id, raw in (data.get("rooms") or {}).items():
            self.rooms[room_id] = RoomState(
                auto=bool(raw.get("auto", True)),
                pin=raw.get("pin"),
                schedules={
                    profile: week_from_json(week)
                    for profile, week in (raw.get("schedules") or {}).items()
                },
            )

    def _as_json(self) -> dict[str, Any]:
        return {
            "base_profile": self.base_profile,
            "enabled": self.enabled,
            "rooms": {
                room_id: {
                    "auto": room.auto,
                    "pin": room.pin,
                    "schedules": {p: week_to_json(w) for p, w in room.schedules.items()},
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

        Creates missing rooms/schedules (copying the room's first existing profile
        or the default week), drops rooms/profiles that no longer exist and repairs
        invalid base profile and pins.
        """
        self.rooms = {rid: self.rooms.get(rid, RoomState()) for rid in room_ids}
        for room in self.rooms.values():
            template = next(iter(room.schedules.values()), None)
            for profile in profiles:
                if profile not in room.schedules:
                    room.schedules[profile] = (
                        {d: list(template[d]) for d in DAYS} if template else default_week()
                    )
            room.schedules = {p: room.schedules[p] for p in profiles}
            if room.pin not in (None, *profiles):
                room.pin = None
        if self.base_profile not in profiles:
            self.base_profile = default_base if default_base in profiles else profiles[0]
