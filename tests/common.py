"""Test helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermostat_schedule.const import DOMAIN

CLIMATE_ATTRS = {"min_temp": 4, "max_temp": 35, "target_temp_step": 0.5}


def set_climate(
    hass: HomeAssistant, entity_id: str, temperature: float, state: str = "heat"
) -> None:
    hass.states.async_set(entity_id, state, {**CLIMATE_ATTRS, "temperature": temperature})


def install_fake_climate_service(hass: HomeAssistant) -> list[tuple[str, float]]:
    """Register climate.set_temperature that echoes the new target like a real TRV."""
    calls: list[tuple[str, float]] = []

    async def handler(call: ServiceCall) -> None:
        entity_id = call.data["entity_id"]
        entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
        calls.append((entity_id, call.data["temperature"]))
        state = hass.states.get(entity_id)
        hass.states.async_set(
            entity_id, state.state, {**state.attributes, "temperature": call.data["temperature"]}
        )

    hass.services.async_register("climate", "set_temperature", handler)
    return calls


def make_entry(
    *,
    rooms: dict[str, dict[str, Any]],
    profiles: list[str] | None = None,
    rules: list[dict[str, Any]] | None = None,
    title: str = "Upper floor",
) -> MockConfigEntry:
    profiles = profiles or ["home", "away", "vacation"]
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"base_profile": profiles[0]},
        options={"profiles": profiles, "rules": rules or []},
        subentries_data=[
            ConfigSubentryData(
                data=data, subentry_type="room", title=data["name"], unique_id=None, subentry_id=sid
            )
            for sid, data in rooms.items()
        ],
    )


OFFICE = {
    "name": "Office",
    "climate_entities": ["climate.office_1", "climate.office_2"],
    "sync_thermostats": True,
    "hold_policy": "next_switch",
    "hold_minutes": 120,
}
