"""Push an external temperature sensor to the room's thermostats (Sonoff TRVZB / Z2M)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_FALLBACK,
    CONF_GRACE_MINUTES,
    CONF_HEARTBEAT_MINUTES,
    CONF_LOCAL_OPTION,
    CONF_MIN_CHANGE,
    CONF_MIN_INTERVAL,
    CONF_REMOTE_OPTION,
    CONF_SENSOR,
    CONF_TRV_NUMBER,
    CONF_TRV_SELECT,
    CONF_TRVS,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_HEARTBEAT_MINUTES,
    DEFAULT_LOCAL_OPTION,
    DEFAULT_MIN_CHANGE,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_REMOTE_OPTION,
    FALLBACK_INTERNAL,
)

_LOGGER = logging.getLogger(__name__)

STATUS_IDLE = "idle"
STATUS_OK = "ok"
STATUS_SENSOR_UNAVAILABLE = "sensor_unavailable"
STATUS_FALLBACK = "fallback"

_NUMBER_MIN = 0.0
_NUMBER_MAX = 99.9
_BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


class ExternalSensorSync:
    """Keeps TRVs' external temperature input and sensor mode in sync with a sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        config: Mapping[str, Any],
        on_status: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._name = name
        self._sensor: str = config[CONF_SENSOR]
        self._trvs: list[Mapping[str, str]] = config[CONF_TRVS]
        self._min_change = float(config.get(CONF_MIN_CHANGE, DEFAULT_MIN_CHANGE))
        self._min_interval = float(config.get(CONF_MIN_INTERVAL, DEFAULT_MIN_INTERVAL))
        self._heartbeat = float(config.get(CONF_HEARTBEAT_MINUTES, DEFAULT_HEARTBEAT_MINUTES))
        self._grace = float(config.get(CONF_GRACE_MINUTES, DEFAULT_GRACE_MINUTES))
        self._fallback: str = config.get(CONF_FALLBACK, FALLBACK_INTERNAL)
        self._remote: str = config.get(CONF_REMOTE_OPTION) or DEFAULT_REMOTE_OPTION
        self._local: str = config.get(CONF_LOCAL_OPTION) or DEFAULT_LOCAL_OPTION
        self._on_status = on_status

        self.status = STATUS_IDLE
        self.last_value: float | None = None
        self._last_sent_at: float | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None
        self._grace_timer: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Start listening and push the current value."""
        watched = [self._sensor] + [
            t[CONF_TRV_SELECT] for t in self._trvs if t.get(CONF_TRV_SELECT)
        ]
        self._unsubs.append(async_track_state_change_event(self._hass, watched, self._handle_event))
        if self._heartbeat > 0:
            self._unsubs.append(
                async_track_time_interval(
                    self._hass, self._handle_heartbeat, timedelta(minutes=self._heartbeat)
                )
            )
        self._hass.async_create_task(self._async_push(force=True))

    @callback
    def async_stop(self) -> None:
        """Stop listening and cancel timers."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in (self._pending, self._grace_timer):
            if cancel:
                cancel()
        self._pending = self._grace_timer = None

    # --- event handling -------------------------------------------------

    @callback
    def _handle_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        if entity_id == self._sensor:
            self._hass.async_create_task(self._async_push(force=False))
        elif (
            self.status == STATUS_OK
            and new is not None
            and new.state not in _BAD_STATES
            and new.state != self._remote
        ):
            # The TRV left external mode (or reports its source as offline): repair.
            self._hass.async_create_task(self._async_push(force=True))

    @callback
    def _handle_heartbeat(self, _now: Any) -> None:
        self._hass.async_create_task(self._async_push(force=True, heartbeat=True))

    # --- core -----------------------------------------------------------

    def _read_sensor(self) -> float | None:
        state = self._hass.states.get(self._sensor)
        if state is None or state.state in _BAD_STATES:
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
            except HomeAssistantError:
                return None
        return value

    def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            self._on_status()

    async def _async_push(self, *, force: bool, heartbeat: bool = False) -> None:
        value = self._read_sensor()
        if value is None:
            if heartbeat and self._fallback != FALLBACK_INTERNAL and self.last_value is not None:
                value = self.last_value  # "keep last value" mode
            else:
                self._handle_sensor_lost()
                return

        recovering = self.status in (STATUS_SENSOR_UNAVAILABLE, STATUS_FALLBACK)
        self._cancel_grace()
        now = time.monotonic()

        if not force and not recovering and self.last_value is not None:
            if abs(value - self.last_value) < self._min_change:
                return

        if self._last_sent_at is not None and now - self._last_sent_at < self._min_interval:
            if self._pending is None:
                delay = self._min_interval - (now - self._last_sent_at)
                self._pending = async_call_later(self._hass, delay, self._handle_pending)
            return

        value = max(_NUMBER_MIN, min(_NUMBER_MAX, round(value, 1)))
        for trv in self._trvs:
            await self._async_write_trv(trv, value)
        self.last_value = value
        self._last_sent_at = time.monotonic()
        self._set_status(STATUS_OK)

    @callback
    def _handle_pending(self, _now: Any) -> None:
        self._pending = None
        self._hass.async_create_task(self._async_push(force=True))

    async def _async_write_trv(self, trv: Mapping[str, str], value: float) -> None:
        number_id, select_id = trv[CONF_TRV_NUMBER], trv.get(CONF_TRV_SELECT)
        number = self._hass.states.get(number_id)
        if number is not None and number.state not in _BAD_STATES:
            await self._async_call(
                "number", "set_value", {ATTR_ENTITY_ID: number_id, "value": value}
            )
        select = self._hass.states.get(select_id) if select_id else None
        if select is not None and select.state not in _BAD_STATES and select.state != self._remote:
            await self._async_call(
                "select",
                "select_option",
                {ATTR_ENTITY_ID: select_id, "option": self._remote},
            )

    async def _async_call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self._hass.services.async_call(domain, service, data, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("%s: %s.%s failed for %s: %s", self._name, domain, service, data, err)

    # --- sensor loss ------------------------------------------------------

    def _handle_sensor_lost(self) -> None:
        if self.status == STATUS_FALLBACK:
            return
        self._set_status(STATUS_SENSOR_UNAVAILABLE)
        if self._grace_timer is None:
            self._grace_timer = async_call_later(
                self._hass, self._grace * 60, self._handle_grace_expired
            )

    @callback
    def _handle_grace_expired(self, _now: Any) -> None:
        self._grace_timer = None
        if self._read_sensor() is not None or self._fallback != FALLBACK_INTERNAL:
            return
        self._hass.async_create_task(self._async_fallback())

    async def _async_fallback(self) -> None:
        for trv in self._trvs:
            select_id = trv.get(CONF_TRV_SELECT)
            select = self._hass.states.get(select_id) if select_id else None
            if select is not None and select.state not in _BAD_STATES:
                await self._async_call(
                    "select",
                    "select_option",
                    {ATTR_ENTITY_ID: select_id, "option": self._local},
                )
        self._set_status(STATUS_FALLBACK)

    def _cancel_grace(self) -> None:
        if self._grace_timer:
            self._grace_timer()
            self._grace_timer = None
