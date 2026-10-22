"""External temperature sensor sync (Sonoff TRVZB via Zigbee2MQTT)."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .common import OFFICE, install_fake_climate_service, make_entry, set_climate

EXTERNAL = {
    "sensor": "sensor.office_temp",
    "trvs": [
        {"climate": "climate.office_1", "number": "number.o1_ext", "select": "select.o1_mode"},
        {"climate": "climate.office_2", "number": "number.o2_ext", "select": "select.o2_mode"},
    ],
    "min_change": 0.1,
    "min_interval": 30,
    "heartbeat_minutes": 15,
    "grace_minutes": 60,
    "fallback": "internal",
}


@pytest.fixture
async def writes(hass: HomeAssistant) -> list[tuple[str, str, object]]:
    # Set the real select component up first so our fake handler is not replaced later.
    assert await async_setup_component(hass, "select", {})
    log: list[tuple[str, str, object]] = []

    async def set_value(call: ServiceCall) -> None:
        log.append(("number", call.data["entity_id"], call.data["value"]))
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]))

    async def select_option(call: ServiceCall) -> None:
        log.append(("select", call.data["entity_id"], call.data["option"]))
        hass.states.async_set(call.data["entity_id"], call.data["option"])

    hass.services.async_register("number", "set_value", set_value)
    hass.services.async_register("select", "select_option", select_option)
    return log


async def setup(hass, freezer, sensor=("20.4", "°C"), **external_overrides):
    freezer.move_to("2026-10-26 06:00:00+00:00")
    install_fake_climate_service(hass)
    for cid in ("climate.office_1", "climate.office_2"):
        set_climate(hass, cid, 20)
    hass.states.async_set("sensor.office_temp", sensor[0], {"unit_of_measurement": sensor[1]})
    for n in ("number.o1_ext", "number.o2_ext"):
        hass.states.async_set(n, "0")
    for s in ("select.o1_mode", "select.o2_mode"):
        hass.states.async_set(s, "local_temperature")
    entry = make_entry(
        rooms={"r": {**OFFICE, "external_sensor": {**EXTERNAL, **external_overrides}}}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def advance(hass, freezer, **delta):
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_initial_push_sets_value_and_remote_mode(hass, freezer, writes) -> None:
    await setup(hass, freezer)
    assert ("number", "number.o1_ext", 20.4) in writes
    assert ("number", "number.o2_ext", 20.4) in writes
    assert ("select", "select.o1_mode", "remote_temperature") in writes
    assert hass.states.get("select.o2_mode").state == "remote_temperature"
    assert hass.states.get("sensor.office_external_temperature_status").state == "ok"


async def test_small_changes_are_ignored_and_large_ones_throttled(hass, freezer, writes) -> None:
    await setup(hass, freezer)
    writes.clear()

    hass.states.async_set("sensor.office_temp", "20.45", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert writes == []  # below min_change

    hass.states.async_set("sensor.office_temp", "21.0", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert writes == []  # within min_interval -> deferred

    await advance(hass, freezer, seconds=31)
    assert ("number", "number.o1_ext", 21.0) in writes


async def test_heartbeat_resends_unchanged_value(hass, freezer, writes) -> None:
    await setup(hass, freezer)
    writes.clear()
    await advance(hass, freezer, minutes=15)
    assert ("number", "number.o1_ext", 20.4) in writes


async def test_trv_leaving_remote_mode_is_repaired(hass, freezer, writes) -> None:
    await setup(hass, freezer)
    await advance(hass, freezer, seconds=40)
    writes.clear()
    hass.states.async_set("select.o1_mode", "remote_source_offline")
    await hass.async_block_till_done()
    assert hass.states.get("select.o1_mode").state == "remote_temperature"


async def test_fallback_to_internal_after_grace_and_recovery(hass, freezer, writes) -> None:
    await setup(hass, freezer)
    hass.states.async_set("sensor.office_temp", "unavailable")
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.office_external_temperature_status").state == "sensor_unavailable"
    )

    await advance(hass, freezer, minutes=61)
    assert hass.states.get("select.o1_mode").state == "local_temperature"
    assert hass.states.get("sensor.office_external_temperature_status").state == "fallback"

    hass.states.async_set("sensor.office_temp", "19.8", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    await advance(hass, freezer, seconds=31)
    assert hass.states.get("select.o1_mode").state == "remote_temperature"
    assert hass.states.get("number.o1_ext").state == "19.8"
    assert hass.states.get("sensor.office_external_temperature_status").state == "ok"


async def test_fahrenheit_sensor_is_converted(hass, freezer, writes) -> None:
    await setup(hass, freezer, sensor=("68", "°F"))
    assert ("number", "number.o1_ext", 20.0) in writes


async def test_custom_mode_options(hass, freezer, writes) -> None:
    for sid in ("select.o1_mode", "select.o2_mode"):
        hass.states.async_set(sid, "internal")
    freezer.move_to("2026-10-26 06:00:00+00:00")
    await setup(hass, freezer, remote_option="external", local_option="internal")
    assert hass.states.get("select.o1_mode").state == "external"

    hass.states.async_set("sensor.office_temp", "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, minutes=61)
    assert hass.states.get("select.o1_mode").state == "internal"


async def test_thermostat_without_mode_select(hass, freezer, writes) -> None:
    trvs = [{"climate": "climate.office_1", "number": "number.o1_ext"}]
    await setup(hass, freezer, trvs=trvs)
    assert ("number", "number.o1_ext", 20.4) in writes
    assert not any(kind == "select" for kind, *_ in writes)
