"""Switches: hub master switch and per-room auto mode."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import HubEntity, RoomEntity
from .hub import ThermostatScheduleConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThermostatScheduleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    hub = entry.runtime_data
    async_add_entities([HubEnabledSwitch(hub)])
    for room_id, controller in hub.controllers.items():
        async_add_entities([RoomAutoSwitch(hub, controller)], config_subentry_id=room_id)


class HubEnabledSwitch(HubEntity, SwitchEntity):
    """Master switch: turns all schedules and rules of the hub on or off."""

    def __init__(self, hub) -> None:
        super().__init__(hub, "enabled")

    @property
    def is_on(self) -> bool:
        return self.hub.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hub.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hub.async_set_enabled(False)


class RoomAutoSwitch(RoomEntity, SwitchEntity):
    """On: the schedule drives the room. Off: purely manual."""

    def __init__(self, hub, controller) -> None:
        super().__init__(hub, controller, "auto")

    @property
    def is_on(self) -> bool:
        return self.controller.room_state.auto

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hub.async_set_room_auto(self.controller.room_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hub.async_set_room_auto(self.controller.room_id, False)
