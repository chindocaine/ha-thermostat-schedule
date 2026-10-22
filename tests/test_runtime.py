"""Runtime behavior: schedule application, overrides, sync, rules, switches."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.thermostat_schedule.models import parse_day

from .common import OFFICE, install_fake_climate_service, make_entry, set_climate

ROOM = "room_office"
# Monday 2026-10-26 08:00 UTC == 09:00 Europe/Berlin (CET): default schedule says 21.5 °C
MONDAY_9 = "2026-10-26 08:00:00+00:00"


@pytest.fixture
async def berlin(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")


async def setup_office(hass, freezer, *, rules=None, extra_rooms=None, start=MONDAY_9):
    freezer.move_to(start)
    calls = install_fake_climate_service(hass)
    set_climate(hass, "climate.office_1", 15)
    set_climate(hass, "climate.office_2", 15)
    entry = make_entry(rooms={ROOM: OFFICE, **(extra_rooms or {})}, rules=rules)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, calls


def target(hass, entity_id):
    return hass.states.get(entity_id).attributes["temperature"]


async def advance(hass, freezer, **delta):
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_applies_schedule_to_all_thermostats_and_follows_it(hass, freezer, berlin) -> None:
    entry, calls = await setup_office(hass, freezer)
    assert target(hass, "climate.office_1") == 21.5
    assert target(hass, "climate.office_2") == 21.5

    await advance(hass, freezer, hours=14)  # 23:00 local
    assert target(hass, "climate.office_1") == 19
    assert target(hass, "climate.office_2") == 19

    state = hass.states.get("sensor.office_target_temperature")
    assert state.state == "19.0"
    assert state.attributes["profile"] == "home"
    assert state.attributes["next_temperature"] == 21.5


async def test_no_redundant_writes(hass, freezer, berlin) -> None:
    entry, calls = await setup_office(hass, freezer)
    assert len(calls) == 2
    await entry.runtime_data.async_reconcile_all()
    assert len(calls) == 2


async def test_manual_change_propagates_and_holds_until_next_switch(hass, freezer, berlin) -> None:
    entry, calls = await setup_office(hass, freezer)
    calls.clear()

    set_climate(hass, "climate.office_1", 23)  # someone turns the dial
    await hass.async_block_till_done()

    assert target(hass, "climate.office_2") == 23
    assert calls == [("climate.office_2", 23.0)]  # no ping-pong back to office_1
    state = hass.states.get("sensor.office_target_temperature")
    assert state.state == "23.0"
    assert state.attributes["override_temperature"] == 23.0

    await advance(hass, freezer, hours=14)  # next switch point 22:30
    assert target(hass, "climate.office_1") == 19
    assert target(hass, "climate.office_2") == 19
    assert (
        hass.states.get("sensor.office_target_temperature").attributes["override_temperature"]
        is None
    )


async def test_resume_button_clears_override(hass, freezer, berlin) -> None:
    await setup_office(hass, freezer)
    set_climate(hass, "climate.office_1", 23)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.office_resume_schedule"}, blocking=True
    )
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 21.5
    assert target(hass, "climate.office_2") == 21.5


async def test_manual_mode_does_not_write_but_still_syncs(hass, freezer, berlin) -> None:
    entry, calls = await setup_office(hass, freezer)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.office_automatic_mode"}, blocking=True
    )
    await hass.async_block_till_done()
    calls.clear()

    await advance(hass, freezer, hours=14)  # schedule would change to 19
    assert calls == []
    assert target(hass, "climate.office_1") == 21.5

    set_climate(hass, "climate.office_1", 20)
    await hass.async_block_till_done()
    assert target(hass, "climate.office_2") == 20  # sync still active

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.office_automatic_mode"}, blocking=True
    )
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 19


async def test_master_switch_stops_everything(hass, freezer, berlin) -> None:
    entry, calls = await setup_office(hass, freezer)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.upper_floor_heating_schedules"}, blocking=True
    )
    await hass.async_block_till_done()
    calls.clear()
    await advance(hass, freezer, hours=14)
    assert calls == []
    assert hass.states.get("sensor.upper_floor_effective_mode").state == "disabled"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.upper_floor_heating_schedules"}, blocking=True
    )
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 19


async def test_rule_switches_profile_and_returns_to_base(hass, freezer, berlin) -> None:
    rules = [
        {
            "id": "away1",
            "name": "Nobody home",
            "scope": "all",
            "entities": ["person.me"],
            "match": "any",
            "state": "not_home",
            "for_minutes": 0,
            "profile": "away",
            "priority": 10,
        }
    ]
    hass.states.async_set("person.me", "home")
    entry, calls = await setup_office(hass, freezer, rules=rules)
    hub = entry.runtime_data
    await hub.async_set_day(ROOM, "away", "mon", parse_day("00:00 15"))
    await hub.async_set_day(ROOM, "vacation", "mon", parse_day("00:00 12"))
    assert target(hass, "climate.office_1") == 21.5

    hass.states.async_set("person.me", "not_home")
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 15
    assert hass.states.get("sensor.office_active_profile").state == "away"

    # Base switched to vacation while away: returning must go to vacation, not home.
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.upper_floor_base_profile", "option": "vacation"},
        blocking=True,
    )
    hass.states.async_set("person.me", "home")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.office_active_profile").state == "vacation"
    assert target(hass, "climate.office_1") == 12


async def test_rule_for_delay(hass, freezer, berlin) -> None:
    rules = [
        {
            "id": "away1",
            "name": "Away",
            "scope": "all",
            "entities": ["person.me"],
            "match": "any",
            "state": "not_home",
            "for_minutes": 10,
            "profile": "away",
            "priority": 10,
        }
    ]
    hass.states.async_set("person.me", "home")
    entry, _ = await setup_office(hass, freezer, rules=rules)
    await entry.runtime_data.async_set_day(ROOM, "away", "mon", parse_day("00:00 15"))
    hass.states.async_set("person.me", "not_home")
    await advance(hass, freezer, minutes=5)
    assert target(hass, "climate.office_1") == 21.5
    await advance(hass, freezer, minutes=6)
    assert target(hass, "climate.office_1") == 15


async def test_room_pin_ignores_hub_rules(hass, freezer, berlin) -> None:
    rules = [
        {
            "id": "away1",
            "name": "Away",
            "scope": "all",
            "entities": ["person.me"],
            "match": "any",
            "state": "not_home",
            "for_minutes": 0,
            "profile": "away",
            "priority": 10,
        }
    ]
    hass.states.async_set("person.me", "home")
    entry, _ = await setup_office(hass, freezer, rules=rules)
    await entry.runtime_data.async_set_day(ROOM, "away", "mon", parse_day("00:00 15"))
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.office_profile", "option": "home"},
        blocking=True,
    )
    hass.states.async_set("person.me", "not_home")
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 21.5


async def test_schedule_edit_applies_immediately(hass, freezer, berlin) -> None:
    entry, _ = await setup_office(hass, freezer)
    await entry.runtime_data.async_set_day(ROOM, "home", "mon", parse_day("00:00 18\n10:00 22"))
    assert target(hass, "climate.office_1") == 18


async def test_thermostat_coming_online_gets_current_value(hass, freezer, berlin) -> None:
    entry, calls = await setup_office(hass, freezer)
    hass.states.async_set("climate.office_2", "unavailable")
    await hass.async_block_till_done()
    calls.clear()
    set_climate(hass, "climate.office_2", 9)
    await hass.async_block_till_done()
    assert target(hass, "climate.office_2") == 21.5


async def test_set_override_service(hass, freezer, berlin) -> None:
    await setup_office(hass, freezer)
    await hass.services.async_call(
        "thermostat_schedule",
        "set_override",
        {
            "entity_id": "sensor.office_target_temperature",
            "temperature": 24,
            "duration": {"minutes": 30},
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 24
    assert target(hass, "climate.office_2") == 24
    await advance(hass, freezer, minutes=31)
    assert target(hass, "climate.office_1") == 21.5


async def test_two_hubs_are_independent(hass, freezer, berlin) -> None:
    entry, _ = await setup_office(hass, freezer)
    other = make_entry(
        rooms={"room_bed": {**OFFICE, "name": "Bedroom", "climate_entities": ["climate.bed"]}},
        title="Ground floor",
    )
    set_climate(hass, "climate.bed", 10)
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    assert target(hass, "climate.bed") == 21.5
    await other.runtime_data.async_set_enabled(False)
    assert entry.runtime_data.enabled is True


async def test_room_presets_drive_the_default_schedule(hass, freezer, berlin) -> None:
    freezer.move_to(MONDAY_9)
    install_fake_climate_service(hass)
    set_climate(hass, "climate.office_1", 10)
    set_climate(hass, "climate.office_2", 10)
    cozy = {**OFFICE, "eco_temperature": 17.0, "comfort_temperature": 23.0}
    entry = make_entry(rooms={ROOM: cozy})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert target(hass, "climate.office_1") == 23.0  # "comfort" at 09:00
    state = hass.states.get("sensor.office_target_temperature")
    assert state.attributes["eco_temperature"] == 17.0
    assert state.attributes["next_temperature"] == 17.0

    await entry.runtime_data.async_set_day(
        ROOM, "home", "mon", parse_day("00:00 eco, 08:00 comfort, 19:00 24, 22:30 eco")
    )
    await advance(hass, freezer, hours=10)  # 19:00
    assert target(hass, "climate.office_1") == 24.0
    await advance(hass, freezer, hours=4)  # 23:00
    assert target(hass, "climate.office_1") == 17.0
