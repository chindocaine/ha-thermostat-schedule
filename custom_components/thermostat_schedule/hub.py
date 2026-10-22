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
    CONF_RULE_SCOPE,
    CONF_RULES,
    DAYS,
    DEFAULT_PROFILES,
    DOMAIN,
    SUBENTRY_ROOM,
)
from .controller import RoomController
from .models import DaySchedule, WeekSchedule, default_week
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
        self.all_rules: list[dict[str, Any]] = []
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

        # Global rules plus every room's own rules, tagged with the room they apply to.
        self.all_rules = [
            *self.rules,
            *(
                {**rule, CONF_RULE_SCOPE: sid}
                for sid, sub in rooms.items()
                for rule in sub.data.get(CONF_RULES, [])
            ),
        ]
        self.rule_engine = RuleEngine(self.hass, self.all_rules, self._handle_rules_changed)
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

    # --- schedules ----------------------------------------------------------------

    def default_week(self, profile: str) -> WeekSchedule:
        """The hub's default week for a profile."""
        return self.store.defaults[profile]

    def effective_week(self, room_id: str, profile: str) -> WeekSchedule:
        """A room's week: its own overrides on top of the hub defaults."""
        overrides = self.store.rooms[room_id].overrides.get(profile, {})
        defaults = self.store.defaults[profile]
        return {day: overrides.get(day, defaults[day]) for day in DAYS}

    def is_override(self, room_id: str, profile: str, day: str) -> bool:
        """Whether a room customizes this day."""
        return day in self.store.rooms[room_id].overrides.get(profile, {})

    async def async_set_default_day(self, profile: str, day: str, points: DaySchedule) -> None:
        """Replace one day of a hub default schedule; all rooms following it update."""
        self.store.defaults[profile][day] = list(points)
        self.store.async_schedule_save()
        await self.async_reconcile_all()

    async def async_set_day(
        self, room_id: str, profile: str, day: str, points: DaySchedule | None
    ) -> None:
        """Override one day for a room (``None`` = back to the hub default)."""
        overrides = self.store.rooms[room_id].overrides
        if points is None:
            overrides.get(profile, {}).pop(day, None)
        else:
            overrides.setdefault(profile, {})[day] = list(points)
        self.store.async_schedule_save()
        await self.controllers[room_id].async_reconcile()

    async def async_reset_room_profile(self, room_id: str, profile: str) -> None:
        """Drop all of a room's overrides for a profile."""
        self.store.rooms[room_id].overrides.pop(profile, None)
        self.store.async_schedule_save()
        await self.controllers[room_id].async_reconcile()

    # --- profile maintenance (used by the options flow) ----------------------------

    def add_profile_schedules(self, name: str, copy_from: str | None) -> None:
        """Create the default schedule (and room overrides) of a new profile."""
        source = self.store.defaults.get(copy_from) if copy_from else None
        self.store.defaults[name] = (
            {d: list(p) for d, p in source.items()} if source else default_week()
        )
        if copy_from:
            for room in self.store.rooms.values():
                if copy_from in room.overrides:
                    room.overrides[name] = {
                        d: list(p) for d, p in room.overrides[copy_from].items()
                    }
        self.store.async_schedule_save()

    def rename_profile_state(self, old: str, new: str) -> None:
        """Rename a profile in all stored state."""
        if self.store.base_profile == old:
            self.store.base_profile = new
        self.store.defaults = {(new if p == old else p): w for p, w in self.store.defaults.items()}
        for room in self.store.rooms.values():
            room.overrides = {(new if p == old else p): d for p, d in room.overrides.items()}
            if room.pin == old:
                room.pin = new
        self.store.async_schedule_save()
