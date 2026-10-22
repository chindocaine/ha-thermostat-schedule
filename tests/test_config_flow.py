"""Config, subentry and options flows."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thermostat_schedule.const import DOMAIN
from custom_components.thermostat_schedule.models import format_day, parse_day

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


async def configure(flow_mgr, result, user_input):
    return await flow_mgr.async_configure(result["flow_id"], user_input)


async def test_hub_edits_default_schedule_for_all_rooms(hass) -> None:
    entry = await create_hub(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await configure(flow, result, {"next_step_id": "schedule"})
    result = await configure(flow, result, {"profile": "away"})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "schedule_day"
    result = await configure(flow, result, {"next_step_id": "edit_day_sat"})
    assert result["step_id"] == "edit_day_sat"

    bad = await configure(flow, result, {"schedule": "nonsense"})
    assert bad["errors"] == {"schedule": "invalid_format"}

    result = await configure(flow, result, {"schedule": "08:00 22\n23:00 16", "copy_to": ["sun"]})
    assert result["step_id"] == "schedule_day"
    week = entry.runtime_data.default_week("away")
    assert week["sat"] == week["sun"]
    assert week["sat"][0].temp == 16.0 and week["sat"][1].minute == 480
    assert week["mon"][1].temp == "comfort"  # untouched
    # the room has no override, so it follows the new default
    assert entry.runtime_data.effective_week("room1", "away")["sat"] == week["sat"]


async def test_default_day_matches_requested_prefill(hass) -> None:
    entry = await create_hub(hass)
    text = format_day(entry.runtime_data.default_week("home")["mon"])
    assert text == "00:00 eco\n08:00 comfort\n22:30 eco"


async def test_room_overrides_single_day_and_can_reset(hass) -> None:
    entry = await create_hub(hass)
    hub = entry.runtime_data
    flow = hass.config_entries.subentries
    result = await flow.async_init(
        (entry.entry_id, "room"), context={"source": "reconfigure", "subentry_id": "room1"}
    )
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "reconfigure"
    result = await configure(flow, result, {"next_step_id": "schedule"})
    result = await configure(flow, result, {"profile": "home"})
    assert result["step_id"] == "schedule_day"
    result = await configure(flow, result, {"next_step_id": "edit_day_mon"})
    result = await configure(
        flow, result, {"schedule": "00:00 18\n09:00 23", "copy_to": ["tue"], "use_default": False}
    )
    assert result["step_id"] == "schedule_day"
    assert hub.is_override("room1", "home", "mon") and hub.is_override("room1", "home", "tue")
    assert not hub.is_override("room1", "home", "wed")
    assert hub.effective_week("room1", "home")["mon"][1].temp == 23.0
    assert hub.default_week("home")["mon"][1].temp == "comfort"  # hub default untouched

    # default change flows through to non-overridden days only
    await hub.async_set_default_day("home", "wed", parse_day("00:00 20"))
    assert hub.effective_week("room1", "home")["wed"][0].temp == 20.0

    result = await configure(flow, result, {"next_step_id": "edit_day_tue"})
    result = await configure(flow, result, {"schedule": "00:00 18", "use_default": True})
    assert not hub.is_override("room1", "home", "tue")
    assert hub.is_override("room1", "home", "mon")

    result = await configure(flow, result, {"next_step_id": "schedule_reset"})
    assert result["step_id"] == "schedule_day"
    assert not hub.is_override("room1", "home", "mon")


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

    result = await flow.async_init(entry.entry_id)
    result = await configure(flow, result, {"next_step_id": "profiles"})
    result = await configure(flow, result, {"next_step_id": "profile_add"})
    result = await configure(flow, result, {"new_name": "guests", "copy_from": "home"})
    assert result["step_id"] == "profiles"
    await hass.async_block_till_done()
    assert "guests" in entry.options["profiles"]
    assert entry.runtime_data.default_week("guests")["mon"][1].temp == "comfort"

    result = await flow.async_init(entry.entry_id)
    result = await configure(flow, result, {"next_step_id": "profiles"})
    result = await configure(flow, result, {"next_step_id": "profile_delete"})
    result = await configure(flow, result, {"profile": "away"})
    assert result["errors"] == {"profile": "profile_in_use"}

    result = await flow.async_init(entry.entry_id)
    result = await configure(flow, result, {"next_step_id": "profiles"})
    result = await configure(flow, result, {"next_step_id": "profile_rename"})
    result = await configure(flow, result, {"profile": "away", "new_name": "out"})
    await hass.async_block_till_done()
    assert entry.options["profiles"] == ["home", "out", "vacation", "guests"]
    assert entry.options["rules"][0]["profile"] == "out"
    assert "out" in entry.runtime_data.store.defaults


async def test_global_rule_add_edit_delete_with_is_not(hass) -> None:
    entry = await create_hub(hass)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await configure(flow, result, {"next_step_id": "rules"})
    result = await configure(flow, result, {"next_step_id": "rule_add"})
    form = {
        "name": "Away",
        "entities": ["person.me"],
        "match": "any",
        "operator": "is_not",
        "states": ["home"],
        "for_minutes": 0,
        "profile": "away",
        "priority": 20,
    }
    bad = await configure(flow, result, {**form, "states": []})
    assert bad["errors"] == {"states": "no_state"}
    result = await configure(flow, result, form)
    assert result["step_id"] == "rules"
    await hass.async_block_till_done()
    (rule,) = entry.options["rules"]
    assert rule["operator"] == "is_not" and rule["states"] == ["home"] and "scope" not in rule

    result = await flow.async_init(entry.entry_id)
    result = await configure(flow, result, {"next_step_id": "rules"})
    result = await configure(flow, result, {"next_step_id": "rule_edit"})
    result = await configure(flow, result, {"rule": rule["id"]})
    assert result["step_id"] == "rule_edit_form"
    result = await configure(flow, result, {**form, "priority": 5, "states": ["home", "work"]})
    await hass.async_block_till_done()
    assert [(r["priority"], r["states"]) for r in entry.options["rules"]] == [(5, ["home", "work"])]
    assert entry.options["rules"][0]["id"] == rule["id"]

    result = await flow.async_init(entry.entry_id)
    result = await configure(flow, result, {"next_step_id": "rules"})
    result = await configure(flow, result, {"next_step_id": "rule_delete"})
    result = await configure(flow, result, {"rule": rule["id"]})
    await hass.async_block_till_done()
    assert entry.options["rules"] == []


async def test_room_rule_applies_to_that_room_only(hass) -> None:
    entry = await create_hub(hass)
    flow = hass.config_entries.subentries
    result = await flow.async_init(
        (entry.entry_id, "room"), context={"source": "reconfigure", "subentry_id": "room1"}
    )
    result = await configure(flow, result, {"next_step_id": "rules"})
    result = await configure(flow, result, {"next_step_id": "rule_add"})
    result = await configure(
        flow,
        result,
        {
            "name": "Office empty",
            "entities": ["binary_sensor.office_occupied"],
            "match": "any",
            "operator": "is",
            "states": ["off"],
            "for_minutes": 0,
            "profile": "away",
            "priority": 10,
        },
    )
    assert result["step_id"] == "rules"
    await hass.async_block_till_done()
    (rule,) = entry.subentries["room1"].data["rules"]
    assert rule["profile"] == "away"
    assert entry.options["rules"] == []  # global rules untouched
    hub = entry.runtime_data
    assert [r["scope"] for r in hub.all_rules] == ["room1"]

    hass.states.async_set("binary_sensor.office_occupied", "off")
    await hass.async_block_till_done()
    assert hub.controllers["room1"].resolution.profile == "away"
    assert hub.hub_resolution().profile == "home"  # hub level ignores room rules


ROOM_RULE = {
    "id": "x",
    "name": "n",
    "entities": ["binary_sensor.x"],
    "match": "any",
    "operator": "is",
    "states": ["on"],
    "for_minutes": 0,
    "profile": "away",
    "priority": 1,
}


async def test_reconfigure_room_settings_keeps_rules(hass) -> None:
    entry = await create_hub(hass)
    sub = entry.subentries["room1"]
    hass.config_entries.async_update_subentry(entry, sub, data={**sub.data, "rules": [ROOM_RULE]})
    await hass.async_block_till_done()
    flow = hass.config_entries.subentries
    result = await flow.async_init(
        (entry.entry_id, "room"), context={"source": "reconfigure", "subentry_id": "room1"}
    )
    result = await configure(flow, result, {"next_step_id": "settings"})
    assert result["step_id"] == "settings"
    result = await configure(
        flow,
        result,
        {
            "name": "Study",
            "climate_entities": ["climate.office_1", "climate.office_2"],
            "sync_thermostats": False,
            "hold_policy": "until_resumed",
            "hold_minutes": 120,
            "use_external": False,
        },
    )
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "reconfigure"
    await hass.async_block_till_done()
    assert entry.subentries["room1"].title == "Study"
    assert entry.subentries["room1"].data["rules"] == [ROOM_RULE]
    assert entry.runtime_data.controllers["room1"].name == "Study"
    result = await configure(flow, result, {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.ABORT


def _old_week(temp, sat=None):
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    week = {d: [{"start": "00:00", "temp": temp}] for d in days}
    if sat:
        week["sat"] = [{"start": "00:00", "temp": sat}]
    return week


async def test_old_storage_layout_is_migrated(hass, hass_storage) -> None:
    room2 = {**OFFICE, "name": "Two", "climate_entities": ["climate.two"]}
    entry = make_entry(rooms={"room1": OFFICE, "room2": room2})
    key = f"thermostat_schedule.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "base_profile": "away",
            "enabled": True,
            "rooms": {
                "room1": {"auto": True, "pin": None, "schedules": {"home": _old_week(20.0)}},
                "room2": {
                    "auto": False,
                    "pin": "home",
                    "schedules": {"home": _old_week(20.0, sat=25.0)},
                },
            },
        },
    }
    install_fake_climate_service(hass)
    for cid in ("climate.office_1", "climate.office_2", "climate.two"):
        set_climate(hass, cid, 10)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.base_profile == "away"
    assert hub.default_week("home")["mon"][0].temp == 20.0  # first room became the default
    assert not hub.is_override("room1", "home", "sat")
    assert hub.is_override("room2", "home", "sat")
    assert hub.effective_week("room2", "home")["sat"][0].temp == 25.0
    assert hub.store.rooms["room2"].auto is False


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
