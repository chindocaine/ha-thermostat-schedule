"""Base entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .controller import RoomController
from .hub import HubRuntime


class HubEntity(Entity):
    """Entity attached to the hub device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: HubRuntime, key: str) -> None:
        self.hub = hub
        self._attr_translation_key = key
        self._attr_unique_id = f"{hub.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, hub.entry.entry_id)}, name=hub.entry.title
        )

    async def async_added_to_hass(self) -> None:
        """Refresh whenever the hub reports a change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.hub.signal, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class RoomEntity(HubEntity):
    """Entity attached to a room device (a subentry)."""

    def __init__(self, hub: HubRuntime, controller: RoomController, key: str) -> None:
        super().__init__(hub, key)
        self.controller = controller
        self._attr_unique_id = f"{controller.room_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controller.room_id)},
            name=controller.name,
            via_device_id=hub.device_id,
        )
