"""Hub runtime: owns store, rule engine and room controllers for one config entry."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_BASE_PROFILE,
    CONF_PROFILES,
    CONF_RULES,
    DEFAULT_PROFILES,
    DOMAIN,
    SUBENTRY_ROOM,
)
from .controller import RoomController
from .models import DaySchedule, WeekSchedule
from .resolver import Resolution, resolve_profile
from .rules import RuleEngine
from .store import HubStore

_LOGGER = logging.getLogger(__name__)

type ThermostatScheduleConfigEntry = ConfigEntry[HubRuntime]


class HubRuntime:
    """Runtime state of one hub (config entry)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = HubStore(hass, entry.entry_id)
        self.controllers: dict[str, RoomController] = {}
        self.rule_engine: RuleEngine | None = None
        self.device_id: str | None = None

    # --- config views -------------------------------------------------------

    @property
    def signal(self) -> str:
        """Dispatcher signal fired on any state change of this hub."""
        return f"{DOMAIN}_update_{self.entry.entry_id}"

    @property
    def profiles(self) -> list[str]:
        """Profile names in configured order."""
        return list(self.entry.options.get(CONF_PROFILES) or DEFAULT_PROFILES)

    @property
    def rules(self) -> list[dict[str, Any]]:
        """Configured trigger rules."""
        return list(self.entry.options.get(CONF_RULES) or [])

    @property
    def enabled(self) -> bool:
        """Master switch state."""
        return self.store.enabled

    @property
    def base_profile(self) -> str:
        """User-selected base profile."""
        assert self.store.base_profile is not None
        return self.store.base_profile

    def hub_resolution(self) -> Resolution:
        """Effective profile at hub level (global rules only)."""
        return resolve_profile(
            base_profile=self.base_profile,
            rules=self.rules,
            active_rule_ids=self.rule_engine.active if self.rule_engine else (),
            room_id=None,
            pin=None,
            available_profiles=self.profiles,
            fallback_profile=self.profiles[0],
        )

    # --- lifecycle ------------------------------------------------------------

    async def async_setup(self) -> None:
        """Load state, start the rule engine and all room controllers."""
        await self.store.async_load()
        rooms = {
            sid: sub
            for sid, sub in self.entry.subentries.items()
            if sub.subentry_type == SUBENTRY_ROOM
        }
        self.store.sync_with_config(
            profiles=self.profiles,
            room_ids=list(rooms),
            default_base=self.entry.data.get(CONF_BASE_PROFILE, self.profiles[0]),
        )
        self.store.async_schedule_save()

        self.rule_engine = RuleEngine(self.hass, self.rules, self._handle_rules_changed)
        self.rule_engine.async_start()
        self.controllers = {sid: RoomController(self, sid, sub.data) for sid, sub in rooms.items()}

    async def async_start(self) -> None:
        """Start controllers (after platforms are set up so entities see first state)."""
        for controller in self.controllers.values():
            await controller.async_start()

    async def async_unload(self) -> None:
        """Stop everything and flush state."""
        for controller in self.controllers.values():
            controller.async_stop()
        if self.rule_engine:
            self.rule_engine.async_stop()
        await self.store.async_save()

    @callback
    def async_notify(self) -> None:
        """Tell entities that state changed."""
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def _handle_rules_changed(self) -> None:
        self.hass.async_create_task(self.async_reconcile_all())

    async def async_reconcile_all(self) -> None:
        """Re-evaluate every room."""
        for controller in self.controllers.values():
            await controller.async_reconcile()
        self.async_notify()

    # --- actions ----------------------------------------------------------------

    async def async_set_enabled(self, enabled: bool) -> None:
        """Turn the whole hub (all schedules and rules) on or off."""
        self.store.enabled = enabled
        self.store.async_schedule_save()
        await self.async_reconcile_all()

    async def async_set_base_profile(self, profile: str) -> None:
        """Select the base profile."""
        if profile not in self.profiles:
            raise ValueError(profile)
        self.store.base_profile = profile
        self.store.async_schedule_save()
        await self.async_reconcile_all()

    async def async_set_room_auto(self, room_id: str, auto: bool) -> None:
        """Switch a room between schedule (auto) and purely manual."""
        room = self.store.rooms[room_id]
        room.auto = auto
        if auto:
            self.controllers[room_id].override = None
        self.store.async_schedule_save()
        await self.controllers[room_id].async_reconcile()

    async def async_set_room_pin(self, room_id: str, profile: str | None) -> None:
        """Pin a room to a profile (None = follow the hub)."""
        self.store.rooms[room_id].pin = profile
        self.store.async_schedule_save()
        await self.controllers[room_id].async_reconcile()

    async def async_set_day(
        self, room_id: str, profile: str, day: str, points: DaySchedule
    ) -> None:
        """Replace one day of a room's profile and apply immediately."""
        self.store.rooms[room_id].schedules[profile][day] = list(points)
        self.store.async_schedule_save()
        await self.controllers[room_id].async_reconcile()

    def get_week(self, room_id: str, profile: str) -> WeekSchedule:
        """Return a room's week for a profile."""
        return self.store.rooms[room_id].schedules[profile]

    # --- profile maintenance (used by the options flow) ----------------------------

    def add_profile_schedules(self, name: str, copy_from: str | None) -> None:
        """Give every room a schedule for a new profile."""
        from .models import default_week

        for room in self.store.rooms.values():
            source = room.schedules.get(copy_from) if copy_from else None
            room.schedules[name] = (
                {d: list(p) for d, p in source.items()} if source else default_week()
            )
        self.store.async_schedule_save()

    def rename_profile_state(self, old: str, new: str) -> None:
        """Rename a profile in all stored state."""
        if self.store.base_profile == old:
            self.store.base_profile = new
        for room in self.store.rooms.values():
            room.schedules = {(new if p == old else p): w for p, w in room.schedules.items()}
            if room.pin == old:
                room.pin = new
        self.store.async_schedule_save()
