"""Sensors: hub effective mode, room temperature/profile and external sensor status."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)

from .const import ATTR_DURATION, ATTR_TEMPERATURE, MAX_TEMP, MIN_TEMP, SERVICE_SET_OVERRIDE
from .entity import HubEntity, RoomEntity
from .external_sensor import STATUS_FALLBACK, STATUS_IDLE, STATUS_OK, STATUS_SENSOR_UNAVAILABLE
from .hub import ThermostatScheduleConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThermostatScheduleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors and the entity service."""
    hub = entry.runtime_data
    async_add_entities([EffectiveModeSensor(hub)])
    for room_id, controller in hub.controllers.items():
        entities: list[HubEntity] = [
            ScheduledTemperatureSensor(hub, controller),
            ActiveProfileSensor(hub, controller),
        ]
        if controller.external:
            entities.append(ExternalStatusSensor(hub, controller))
        async_add_entities(entities, config_subentry_id=room_id)

    async_get_current_platform().async_register_entity_service(
        SERVICE_SET_OVERRIDE,
        {
            vol.Required(ATTR_TEMPERATURE): vol.All(
                vol.Coerce(float), vol.Range(min=MIN_TEMP, max=MAX_TEMP)
            ),
            vol.Optional(ATTR_DURATION): cv.positive_time_period_dict,
        },
        "async_service_set_override",
    )


class EffectiveModeSensor(HubEntity, SensorEntity):
    """Hub-level effective profile (global rules only), or ``disabled``."""

    def __init__(self, hub) -> None:
        super().__init__(hub, "effective_mode")

    @property
    def native_value(self) -> str:
        if not self.hub.enabled:
            return "disabled"
        return self.hub.hub_resolution().profile

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        resolution = self.hub.hub_resolution()
        return {
            "reason": resolution.reason,
            "base_profile": self.hub.base_profile,
            "active_rules": sorted(self.hub.rule_engine.active) if self.hub.rule_engine else [],
        }


class ScheduledTemperatureSensor(RoomEntity, SensorEntity):
    """The room's current target temperature (override or schedule)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, hub, controller) -> None:
        super().__init__(hub, controller, "target_temperature")

    @property
    def native_value(self) -> float | None:
        return self.controller.target_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.controller
        return {
            "scheduled_temperature": c.scheduled_temp,
            "profile": c.resolution.profile if c.resolution else None,
            "reason": c.resolution.reason if c.resolution else None,
            "schedule_active": c.active,
            "override_temperature": c.override.temp if c.override else None,
            "override_until": c.override.until.isoformat()
            if c.override and c.override.until
            else None,
            "next_change": c.next_change_at.isoformat() if c.next_change_at else None,
            "next_temperature": c.next_temp,
            "eco_temperature": c.presets["eco"],
            "comfort_temperature": c.presets["comfort"],
            "thermostats": c.climates,
        }

    async def async_service_set_override(self, temperature: float, duration: Any = None) -> None:
        """Entity service: set a room-wide manual temperature."""
        minutes = duration.total_seconds() / 60 if duration else None
        await self.controller.async_set_override(temperature, minutes)


class ActiveProfileSensor(RoomEntity, SensorEntity):
    """Name of the profile currently used by the room."""

    def __init__(self, hub, controller) -> None:
        super().__init__(hub, controller, "active_profile")

    @property
    def native_value(self) -> str | None:
        res = self.controller.resolution
        return res.profile if res else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        res = self.controller.resolution
        return {"reason": res.reason if res else None}


class ExternalStatusSensor(RoomEntity, SensorEntity):
    """Status of the external temperature sync."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_IDLE, STATUS_OK, STATUS_SENSOR_UNAVAILABLE, STATUS_FALLBACK]

    def __init__(self, hub, controller) -> None:
        super().__init__(hub, controller, "external_temperature_status")

    @property
    def native_value(self) -> str:
        return self.controller.external.status  # type: ignore[union-attr]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"last_sent_temperature": self.controller.external.last_value}  # type: ignore[union-attr]
