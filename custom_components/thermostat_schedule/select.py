"""Selects: hub base profile and per-room profile pin."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import PIN_FOLLOW_HUB
from .entity import HubEntity, RoomEntity
from .hub import ThermostatScheduleConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThermostatScheduleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects."""
    hub = entry.runtime_data
    async_add_entities([BaseProfileSelect(hub)])
    for room_id, controller in hub.controllers.items():
        async_add_entities([RoomProfileSelect(hub, controller)], config_subentry_id=room_id)


class BaseProfileSelect(HubEntity, SelectEntity):
    """The base profile (e.g. home / vacation) that rules overlay."""

    def __init__(self, hub) -> None:
        super().__init__(hub, "base_profile")
        self._attr_options = hub.profiles

    @property
    def current_option(self) -> str:
        return self.hub.base_profile

    async def async_select_option(self, option: str) -> None:
        await self.hub.async_set_base_profile(option)


class RoomProfileSelect(RoomEntity, SelectEntity):
    """Follow the hub, or pin this room to one profile."""

    def __init__(self, hub, controller) -> None:
        super().__init__(hub, controller, "profile")
        self._attr_options = [PIN_FOLLOW_HUB, *hub.profiles]

    @property
    def current_option(self) -> str:
        return self.controller.room_state.pin or PIN_FOLLOW_HUB

    async def async_select_option(self, option: str) -> None:
        pin = None if option == PIN_FOLLOW_HUB else option
        await self.hub.async_set_room_pin(self.controller.room_id, pin)
