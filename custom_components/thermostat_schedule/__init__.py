"""Thermostat Schedule: HomeMatic-IP-style weekly heating profiles for climate entities."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .hub import HubRuntime, ThermostatScheduleConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ThermostatScheduleConfigEntry) -> bool:
    """Set up a hub."""
    hub = HubRuntime(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub

    # Create the hub device up front so room devices can link to it.
    hub_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
    )
    hub.device_id = hub_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()

    # Rooms, profiles and rules live in the entry/subentries: reload on any change.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ThermostatScheduleConfigEntry) -> bool:
    """Unload a hub."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ThermostatScheduleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
