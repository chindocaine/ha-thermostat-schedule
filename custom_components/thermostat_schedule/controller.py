"""Per-room controller: applies the schedule, handles overrides and TRV sync."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CLIMATES,
    CONF_EXTERNAL,
    CONF_HOLD_MINUTES,
    CONF_HOLD_POLICY,
    CONF_NAME,
    CONF_SYNC,
    DEFAULT_HOLD_MINUTES,
    EXPECTED_WRITE_TTL,
    EXTERNAL_DEBOUNCE,
    HOLD_DURATION,
    HOLD_IGNORE,
    HOLD_NEXT_SWITCH,
    HOLD_UNTIL_RESUMED,
)
from .external_sensor import ExternalSensorSync
from .resolver import Resolution, resolve_profile
from .schedule import next_change, next_switch_time, setpoint_at

if TYPE_CHECKING:
    from .hub import HubRuntime

_LOGGER = logging.getLogger(__name__)

_BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)
_TOLERANCE = 0.05
_ECHO_TOLERANCE = 0.26  # TRVs round to 0.5 K steps


@dataclass
class Override:
    """A manual setpoint that temporarily replaces the schedule."""

    temp: float
    until: datetime | None  # None -> until resumed


class RoomController:
    """Drives all thermostats of one room from the hub's schedule."""

    def __init__(self, hub: HubRuntime, room_id: str, config: Mapping[str, Any]) -> None:
        self.hub = hub
        self.room_id = room_id
        self.name: str = config[CONF_NAME]
        self.climates: list[str] = list(config[CONF_CLIMATES])
        self._sync: bool = config.get(CONF_SYNC, True)
        self._hold_policy: str = config.get(CONF_HOLD_POLICY, HOLD_NEXT_SWITCH)
        self._hold_minutes: int = int(config.get(CONF_HOLD_MINUTES, DEFAULT_HOLD_MINUTES))
        self.external: ExternalSensorSync | None = None
        if external := config.get(CONF_EXTERNAL):
            self.external = ExternalSensorSync(hub.hass, self.name, external, hub.async_notify)

        self.override: Override | None = None
        self.resolution: Resolution | None = None
        self.scheduled_temp: float | None = None
        self.target_temp: float | None = None
        self.next_change_at: datetime | None = None
        self.next_temp: float | None = None

        self._lock = asyncio.Lock()
        self._expected: dict[str, tuple[float, float]] = {}
        self._last_external: tuple[float, float] | None = None  # (monotonic, value)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timer: CALLBACK_TYPE | None = None

    # --- state ------------------------------------------------------------

    @property
    def room_state(self):
        """Persistent state of this room."""
        return self.hub.store.rooms[self.room_id]

    @property
    def active(self) -> bool:
        """Whether the schedule currently drives this room."""
        return self.hub.enabled and self.room_state.auto

    # --- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        """Subscribe and apply the schedule."""
        self._unsubs.append(
            async_track_state_change_event(self.hub.hass, self.climates, self._handle_climate)
        )
        if self.external:
            self.external.async_start()
        await self.async_reconcile()

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and cancel timers."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._timer:
            self._timer()
            self._timer = None
        if self.external:
            self.external.async_stop()

    # --- reconcile ----------------------------------------------------------

    async def async_reconcile(self) -> None:
        """Recompute profile/setpoint, apply it, and re-arm the timer."""
        async with self._lock:
            await self._async_reconcile_locked()

    async def _async_reconcile_locked(self) -> None:
        hub = self.hub
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()

        resolution = resolve_profile(
            base_profile=hub.base_profile,
            rules=hub.rules,
            active_rule_ids=hub.rule_engine.active,
            room_id=self.room_id,
            pin=self.room_state.pin,
            available_profiles=self.room_state.schedules,
            fallback_profile=hub.profiles[0],
        )
        if (
            self.resolution is not None
            and resolution.profile != self.resolution.profile
            and self._hold_policy != HOLD_UNTIL_RESUMED
        ):
            self.override = None  # a profile change cancels a temporary manual value
        self.resolution = resolution

        week = self.room_state.schedules[resolution.profile]
        self.scheduled_temp = setpoint_at(week, now, tz)
        change = next_change(week, now, tz)
        self.next_change_at, self.next_temp = change if change else (None, None)

        if self.override and self.override.until and now >= self.override.until:
            self.override = None
        self.target_temp = self.override.temp if self.override else self.scheduled_temp

        if self.active:
            for entity_id in self.climates:
                await self._async_write(entity_id, self.target_temp)

        self._arm_timer(next_switch_time(week, now, tz))
        hub.async_notify()

    def _arm_timer(self, next_switch: datetime | None) -> None:
        if self._timer:
            self._timer()
            self._timer = None
        candidates = [next_switch]
        if self.override and self.override.until:
            candidates.append(self.override.until)
        when = min((c for c in candidates if c), default=None)
        if when is not None:
            self._timer = async_track_point_in_time(self.hub.hass, self._handle_timer, when)

    @callback
    def _handle_timer(self, _now: datetime) -> None:
        self._timer = None
        self.hub.hass.async_create_task(self.async_reconcile())

    # --- writing ------------------------------------------------------------

    def _snap(self, entity_id: str, value: float) -> float:
        state = self.hub.hass.states.get(entity_id)
        attrs = state.attributes if state else {}
        step = float(attrs.get("target_temp_step") or 0.5)
        snapped = round(round(value / step) * step, 2)
        low = attrs.get("min_temp")
        high = attrs.get("max_temp")
        if low is not None:
            snapped = max(float(low), snapped)
        if high is not None:
            snapped = min(float(high), snapped)
        return snapped

    async def _async_write(self, entity_id: str, value: float) -> None:
        """Write ``value`` to a climate if it is reachable and differs."""
        hass = self.hub.hass
        state = hass.states.get(entity_id)
        if state is None or state.state in _BAD_STATES:
            return
        target = self._snap(entity_id, value)
        current = state.attributes.get("temperature")
        if current is not None and abs(float(current) - target) < _TOLERANCE:
            return
        expected = self._expected.get(entity_id)
        if expected and abs(expected[0] - target) < _TOLERANCE and time.monotonic() < expected[1]:
            return  # identical write still in flight
        self._expected[entity_id] = (target, time.monotonic() + EXPECTED_WRITE_TTL)
        try:
            await hass.services.async_call(
                "climate",
                "set_temperature",
                {ATTR_ENTITY_ID: entity_id, "temperature": target},
                blocking=True,
            )
        except HomeAssistantError as err:
            self._expected.pop(entity_id, None)
            _LOGGER.warning("%s: could not set %s to %s: %s", self.name, entity_id, target, err)

    def _is_expected(self, entity_id: str, value: float) -> bool:
        expected = self._expected.get(entity_id)
        if expected is None:
            return False
        if time.monotonic() >= expected[1]:
            self._expected.pop(entity_id, None)
            return False
        if abs(expected[0] - value) < _ECHO_TOLERANCE:
            self._expected.pop(entity_id, None)
            return True
        return False

    # --- climate events -------------------------------------------------------

    @callback
    def _handle_climate(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or new.state in _BAD_STATES:
            return
        if old is None or old.state in _BAD_STATES:
            # Appeared or came back online: bring it in line with the room.
            self.hub.hass.async_create_task(self.async_reconcile())
            return
        new_temp = new.attributes.get("temperature")
        if new_temp is None or new_temp == old.attributes.get("temperature"):
            return
        value = float(new_temp)
        if self._is_expected(entity_id, value):
            return
        self.hub.hass.async_create_task(self._async_external_change(entity_id, value))

    async def _async_external_change(self, entity_id: str, value: float) -> None:
        """A thermostat setpoint changed by someone else (dial, app, Z2M, ...)."""
        async with self._lock:
            now_m = time.monotonic()
            last = self._last_external
            if last and now_m - last[0] < EXTERNAL_DEBOUNCE and abs(last[1] - value) >= _TOLERANCE:
                return  # several TRVs reported at once: first change wins
            self._last_external = (now_m, value)

            if self.active:
                if self._hold_policy == HOLD_IGNORE:
                    await self._async_reconcile_locked()
                    return
                self._apply_override(value)
            if self._sync:
                for other in self.climates:
                    if other != entity_id:
                        await self._async_write(other, value)
            if self.active:
                self._arm_timer(self._next_switch())
            self.hub.async_notify()

    def _next_switch(self) -> datetime | None:
        week = self.room_state.schedules[self.resolution.profile]  # type: ignore[union-attr]
        return next_switch_time(week, dt_util.utcnow(), dt_util.get_default_time_zone())

    def _apply_override(self, value: float, minutes: float | None = None) -> None:
        """Create (or clear, if equal to the schedule) a manual override."""
        if self.scheduled_temp is not None and abs(value - self.scheduled_temp) < _TOLERANCE:
            self.override = None
            self.target_temp = self.scheduled_temp
            return
        now = dt_util.utcnow()
        if minutes is not None:
            until: datetime | None = now + timedelta(minutes=minutes)
        elif self._hold_policy == HOLD_DURATION:
            until = now + timedelta(minutes=self._hold_minutes)
        elif self._hold_policy == HOLD_UNTIL_RESUMED:
            until = None
        else:
            until = self._next_switch()
        self.override = Override(value, until)
        self.target_temp = value

    # --- public actions ---------------------------------------------------------

    async def async_resume(self) -> None:
        """Drop any manual override and return to the schedule."""
        self.override = None
        await self.async_reconcile()

    async def async_set_override(self, temperature: float, minutes: float | None = None) -> None:
        """Set a room-wide manual temperature (service)."""
        async with self._lock:
            if self.active:
                self._apply_override(temperature, minutes)
                self.target_temp = temperature
            for entity_id in self.climates:
                await self._async_write(entity_id, temperature)
            if self.active:
                self._arm_timer(self._next_switch())
            self.hub.async_notify()
