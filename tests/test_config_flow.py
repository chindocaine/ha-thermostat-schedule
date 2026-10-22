"""Config, subentry and options flows."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermostat_schedule.const import DOMAIN

from .common import OFFICE, install_fake_climate_service, make_entry, set_climate


async def create_hub(hass, **kwargs):
    entry = make_entry(rooms={"room1": OFFICE}, **kwargs)
    install_fake_climate_service(hass)
    set_climate(hass, "climate.office_1", 20)
    set_climate(hass, "climate.office_2", 20)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_user_flow_creates_hub_and_allows_second(hass) -> None:
    for name in ("Ground floor", "Upper floor"):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": name, "profiles": ["comfort", " eco ", "Eco"]}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == name
        assert result["options"]["profiles"] == ["comfort", "eco"]
        assert result["data"]["base_profile"] == "comfort"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_user_flow_requires_profiles(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "x", "profiles": ["  "]}
    )
    assert result["errors"] == {"profiles": "invalid_profiles"}


async def test_add_room_with_external_sensor_autodetects_entities(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"base_profile": "home"}, options={"profiles": ["home"], "rules": []}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "trv")}
    )
    registry.async_get_or_create(
        "climate", "test", "trv_climate", suggested_object_id="trv", device_id=device.id
    )
    registry.async_get_or_create(
        "number",
        "test",
        "trv_external_temperature_input",
        suggested_object_id="trv_ext",
        device_id=device.id,
    )
    registry.async_get_or_create(
        "select",
        "test",
        "trv_temperature_sensor_select",
        suggested_object_id="trv_mode",
        device_id=device.id,
    )
    set_climate(hass, "climate.trv", 20)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Office",
            "climate_entities": ["climate.trv"],
            "sync_thermostats": True,
            "hold_policy": "next_switch",
            "hold_minutes": 120,
            "use_external": True,
        },
    )
    assert result["step_id"] == "external"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "sensor": "sensor.office_temp",
            "min_change": 0.1,
            "min_interval": 30,
            "heartbeat_minutes": 15,
            "grace_minutes": 60,
            "fallback": "internal",
            "remote_option": "remote_temperature",
            "local_option": "local_temperature",
        },
    )
    assert result["step_id"] == "external_trv"
    schema_keys = {str(k): k for k in result["data_schema"].schema}
    assert schema_keys["number"].description["suggested_value"] == "number.trv_ext"
    assert schema_keys["select"].description["suggested_value"] == "select.trv_mode"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"number": "number.trv_ext", "select": "select.trv_mode"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    (sub,) = entry.subentries.values()
    assert sub.data["external_sensor"]["trvs"] == [
        {"climate": "climate.trv", "number": "number.trv_ext", "select": "select.trv_mode"}
    ]


async def test_room_rejects_thermostat_used_elsewhere(hass) -> None:
    entry = await create_hub(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Dup",
            "climate_entities": ["climate.office_1"],
            "sync_thermostats": True,
            "hold_policy": "next_switch",
            "hold_minutes": 120,
            "use_external": False,
        },
    )
    assert result["errors"] == {"climate_entities": "climate_in_use"}


async def test_options_edit_day_with_copy(hass) -> None:
    entry = await create_hub(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "schedule"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"room": "room1"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "away"}
    )
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "schedule_day"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_day_sat"}
    )
    assert result["step_id"] == "edit_day_sat"

    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {"schedule": "nonsense"}
    )
    assert bad["errors"] == {"schedule": "invalid_format"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"schedule": "08:00 22\n23:00 16", "copy_to": ["sun"]}
    )
    assert result["step_id"] == "schedule_day"
    week = entry.runtime_data.get_week("room1", "away")
    assert week["sat"] == week["sun"]
    assert week["sat"][0].temp == 16.0 and week["sat"][1].minute == 480
    assert week["mon"][1].temp == 21.0  # untouched


async def test_options_profile_add_rename_delete_with_rule_guard(hass) -> None:
    rule = {
        "id": "r1",
        "name": "Away",
        "scope": "all",
        "entities": ["person.me"],
        "match": "any",
        "state": "not_home",
        "for_minutes": 0,
        "profile": "away",
        "priority": 10,
    }
    entry = await create_hub(hass, rules=[rule])
    flow = hass.config_entries.options

    async def step(result, user_input):
        return await flow.async_configure(result["flow_id"], user_input)

    result = await flow.async_init(entry.entry_id)
    result = await step(result, {"next_step_id": "profiles"})
    result = await step(result, {"next_step_id": "profile_add"})
    result = await step(result, {"new_name": "guests", "copy_from": "home"})
    assert result["step_id"] == "profiles"
    await hass.async_block_till_done()
    assert "guests" in entry.options["profiles"]
    assert entry.runtime_data.get_week("room1", "guests")["mon"][1].temp == 21.0

    result = await flow.async_init(entry.entry_id)
    result = await step(result, {"next_step_id": "profiles"})
    result = await step(result, {"next_step_id": "profile_delete"})
    result = await step(result, {"profile": "away"})
    assert result["errors"] == {"profile": "profile_in_use"}

    result = await flow.async_init(entry.entry_id)
    result = await step(result, {"next_step_id": "profiles"})
    result = await step(result, {"next_step_id": "profile_rename"})
    result = await step(result, {"profile": "away", "new_name": "out"})
    await hass.async_block_till_done()
    assert entry.options["profiles"] == ["home", "out", "vacation", "guests"]
    assert entry.options["rules"][0]["profile"] == "out"
    assert "out" in entry.runtime_data.store.rooms["room1"].schedules


async def test_options_rule_add_edit_delete(hass) -> None:
    entry = await create_hub(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "rules"})
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "rule_add"})
    form = {
        "name": "Vacation",
        "scope": "all",
        "entities": ["input_boolean.vacation"],
        "match": "any",
        "state": "on",
        "for_minutes": 0,
        "profile": "vacation",
        "priority": 20,
    }
    result = await flow.async_configure(result["flow_id"], form)
    assert result["step_id"] == "rules"
    await hass.async_block_till_done()
    (rule,) = entry.options["rules"]
    assert rule["profile"] == "vacation" and rule["priority"] == 20

    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "rules"})
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "rule_edit"})
    result = await flow.async_configure(result["flow_id"], {"rule": rule["id"]})
    assert result["step_id"] == "rule_edit_form"
    result = await flow.async_configure(result["flow_id"], {**form, "priority": 5})
    await hass.async_block_till_done()
    assert [r["priority"] for r in entry.options["rules"]] == [5]
    assert entry.options["rules"][0]["id"] == rule["id"]

    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "rules"})
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "rule_delete"})
    result = await flow.async_configure(result["flow_id"], {"rule": rule["id"]})
    await hass.async_block_till_done()
    assert entry.options["rules"] == []


async def test_unload_and_reload_preserves_state(hass) -> None:
    entry = await create_hub(hass)
    await entry.runtime_data.async_set_base_profile("vacation")
    await entry.runtime_data.async_set_room_auto("room1", False)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.base_profile == "vacation"
    assert entry.runtime_data.store.rooms["room1"].auto is False


async def test_adding_a_room_to_a_running_hub_reloads_it(hass) -> None:
    entry = await create_hub(hass)
    set_climate(hass, "climate.new", 10)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Bath",
            "climate_entities": ["climate.new"],
            "sync_thermostats": True,
            "hold_policy": "duration",
            "hold_minutes": 60,
            "use_external": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.runtime_data.controllers) == 2
    assert hass.states.get("switch.bath_automatic_mode") is not None


async def test_reconfigure_room(hass) -> None:
    entry = await create_hub(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"),
        context={"source": "reconfigure", "subentry_id": "room1"},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Study",
            "climate_entities": ["climate.office_1", "climate.office_2"],
            "sync_thermostats": False,
            "hold_policy": "until_resumed",
            "hold_minutes": 120,
            "use_external": False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    assert entry.subentries["room1"].title == "Study"
    assert entry.runtime_data.controllers["room1"].name == "Study"
