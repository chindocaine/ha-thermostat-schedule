"""Button: resume the schedule after a manual override."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoomEntity
from .hub import ThermostatScheduleConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThermostatScheduleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    hub = entry.runtime_data
    for room_id, controller in hub.controllers.items():
        async_add_entities([ResumeButton(hub, controller)], config_subentry_id=room_id)


class ResumeButton(RoomEntity, ButtonEntity):
    """Drops the manual override of a room."""

    def __init__(self, hub, controller) -> None:
        super().__init__(hub, controller, "resume_schedule")

    async def async_press(self) -> None:
        await self.controller.async_resume()
