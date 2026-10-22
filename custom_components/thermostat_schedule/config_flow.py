"""Config flow: hub setup, room subentries and the hub options flow."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_BASE_PROFILE,
    CONF_CLIMATES,
    CONF_EXTERNAL,
    CONF_FALLBACK,
    CONF_GRACE_MINUTES,
    CONF_HEARTBEAT_MINUTES,
    CONF_HOLD_MINUTES,
    CONF_HOLD_POLICY,
    CONF_LOCAL_OPTION,
    CONF_MIN_CHANGE,
    CONF_MIN_INTERVAL,
    CONF_NAME,
    CONF_PROFILES,
    CONF_REMOTE_OPTION,
    CONF_RULE_ENTITIES,
    CONF_RULE_FOR_MINUTES,
    CONF_RULE_ID,
    CONF_RULE_MATCH,
    CONF_RULE_NAME,
    CONF_RULE_PRIORITY,
    CONF_RULE_PROFILE,
    CONF_RULE_SCOPE,
    CONF_RULE_STATE,
    CONF_RULES,
    CONF_SENSOR,
    CONF_SYNC,
    CONF_TRV_CLIMATE,
    CONF_TRV_NUMBER,
    CONF_TRV_SELECT,
    CONF_TRVS,
    DAYS,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_HEARTBEAT_MINUTES,
    DEFAULT_HOLD_MINUTES,
    DEFAULT_LOCAL_OPTION,
    DEFAULT_MIN_CHANGE,
    DEFAULT_MIN_INTERVAL,
    DEFAULT_PROFILES,
    DEFAULT_REMOTE_OPTION,
    DOMAIN,
    FALLBACK_INTERNAL,
    FALLBACK_KEEP,
    HOLD_NEXT_SWITCH,
    HOLD_POLICIES,
    MATCH_ALL,
    MATCH_ANY,
    SCOPE_ALL,
    SUBENTRY_ROOM,
)
from .hub import HubRuntime
from .models import ScheduleError, format_day, format_minute, format_temp, parse_day

CONF_COPY_FROM = "copy_from"
CONF_NEW_NAME = "new_name"
CONF_PROFILE = "profile"
CONF_USE_EXTERNAL = "use_external"
CONF_SCHEDULE_TEXT = "schedule"
CONF_COPY_TO = "copy_to"
CONF_ROOM = "room"
CONF_RULE = "rule"
MAX_PROFILE_NAME = 40


def _clean_profiles(raw: list[str]) -> list[str] | None:
    """Strip and de-duplicate (case-insensitively); None if empty or too long."""
    seen: dict[str, str] = {}
    for name in raw:
        name = name.strip()
        if not name:
            continue
        if len(name) > MAX_PROFILE_NAME:
            return None
        seen.setdefault(name.casefold(), name)
    return list(seen.values()) or None


def _select(options: list[str], *, key: str | None = None, **kwargs: Any) -> SelectSelector:
    config: dict[str, Any] = {
        "options": options,
        "mode": kwargs.pop("mode", SelectSelectorMode.DROPDOWN),
        **kwargs,
    }
    if key:
        config["translation_key"] = key
    return SelectSelector(SelectSelectorConfig(**config))


def _labeled(options: dict[str, str]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=v, label=label) for v, label in options.items()],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _number(low: float, high: float, step: float, unit: str | None = None) -> NumberSelector:
    config: dict[str, Any] = {
        "min": low,
        "max": high,
        "step": step,
        "mode": NumberSelectorMode.BOX,
    }
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(NumberSelectorConfig(**config))


# --------------------------------------------------------------------------------
# Hub config flow
# --------------------------------------------------------------------------------


class ThermostatScheduleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a hub. Add the integration again for another hub (e.g. per floor)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            profiles = _clean_profiles(user_input[CONF_PROFILES])
            if profiles is None:
                errors[CONF_PROFILES] = "invalid_profiles"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or "Thermostat Schedule",
                    data={CONF_BASE_PROFILE: profiles[0]},
                    options={CONF_PROFILES: profiles, CONF_RULES: []},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Thermostat Schedule"): TextSelector(),
                vol.Required(CONF_PROFILES, default=DEFAULT_PROFILES): SelectSelector(
                    SelectSelectorConfig(
                        options=DEFAULT_PROFILES,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ROOM: RoomSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HubOptionsFlow()


# --------------------------------------------------------------------------------
# Room subentry flow
# --------------------------------------------------------------------------------


def _discover_trv_entities(hass: HomeAssistant, climate_id: str) -> tuple[str | None, str | None]:
    """Find the TRVZB external-temperature number and sensor-select on the same device."""
    registry = er.async_get(hass)
    entry = registry.async_get(climate_id)
    if entry is None or entry.device_id is None:
        return None, None
    number = select = None
    for sibling in er.async_entries_for_device(registry, entry.device_id):
        haystack = f"{sibling.entity_id} {sibling.unique_id}".lower()
        if sibling.domain == "number" and "external_temperature_input" in haystack:
            number = sibling.entity_id
        elif sibling.domain == "select" and "temperature_sensor_select" in haystack:
            select = sibling.entity_id
    return number, select


class RoomSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a room."""

    def __init__(self) -> None:
        self._room: dict[str, Any] = {}
        self._external: dict[str, Any] = {}
        self._trv_index = 0
        self._trvs: list[dict[str, str]] = []
        self._existing_trvs: dict[str, dict[str, str]] = {}
        self._existing_external: dict[str, Any] = {}

    # -- entry points --------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_room_form("user", user_input, {})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        data = dict(self._get_reconfigure_subentry().data)
        self._existing_trvs = {
            t[CONF_TRV_CLIMATE]: t for t in (data.get(CONF_EXTERNAL) or {}).get(CONF_TRVS, [])
        }
        self._existing_external = data.get(CONF_EXTERNAL) or {}
        return await self._async_room_form(
            "reconfigure", user_input, {**data, CONF_USE_EXTERNAL: bool(data.get(CONF_EXTERNAL))}
        )

    # -- steps -----------------------------------------------------------------

    def _climates_in_use(self) -> set[str]:
        own = self._get_reconfigure_subentry().subentry_id if self.source == "reconfigure" else None
        used: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            for sub in entry.subentries.values():
                if sub.subentry_type == SUBENTRY_ROOM and sub.subentry_id != own:
                    used.update(sub.data.get(CONF_CLIMATES, []))
        return used

    async def _async_room_form(
        self, step_id: str, user_input: dict[str, Any] | None, defaults: dict[str, Any]
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_CLIMATES]:
                errors[CONF_CLIMATES] = "no_thermostat"
            elif set(user_input[CONF_CLIMATES]) & self._climates_in_use():
                errors[CONF_CLIMATES] = "climate_in_use"
            if not errors:
                self._room = {
                    CONF_NAME: user_input[CONF_NAME].strip(),
                    CONF_CLIMATES: user_input[CONF_CLIMATES],
                    CONF_SYNC: user_input[CONF_SYNC],
                    CONF_HOLD_POLICY: user_input[CONF_HOLD_POLICY],
                    CONF_HOLD_MINUTES: int(user_input[CONF_HOLD_MINUTES]),
                }
                if user_input[CONF_USE_EXTERNAL]:
                    return await self.async_step_external()
                return self._finish()
            defaults = user_input

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_CLIMATES): EntitySelector(
                    EntitySelectorConfig(domain="climate", multiple=True)
                ),
                vol.Required(CONF_SYNC, default=True): BooleanSelector(),
                vol.Required(CONF_HOLD_POLICY, default=HOLD_NEXT_SWITCH): _select(
                    list(HOLD_POLICIES), key="hold_policy"
                ),
                vol.Required(CONF_HOLD_MINUTES, default=DEFAULT_HOLD_MINUTES): _number(
                    1, 1440, 1, "min"
                ),
                vol.Required(CONF_USE_EXTERNAL, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors,
        )

    async def async_step_external(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        previous = self._existing_external
        if user_input is not None:
            self._external = {
                CONF_SENSOR: user_input[CONF_SENSOR],
                CONF_MIN_CHANGE: float(user_input[CONF_MIN_CHANGE]),
                CONF_MIN_INTERVAL: float(user_input[CONF_MIN_INTERVAL]),
                CONF_HEARTBEAT_MINUTES: float(user_input[CONF_HEARTBEAT_MINUTES]),
                CONF_GRACE_MINUTES: float(user_input[CONF_GRACE_MINUTES]),
                CONF_FALLBACK: user_input[CONF_FALLBACK],
                CONF_REMOTE_OPTION: user_input[CONF_REMOTE_OPTION].strip(),
                CONF_LOCAL_OPTION: user_input[CONF_LOCAL_OPTION].strip(),
            }
            self._trv_index = 0
            self._trvs = []
            return await self.async_step_external_trv()

        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Required(CONF_MIN_CHANGE, default=DEFAULT_MIN_CHANGE): _number(0, 5, 0.1, "K"),
                vol.Required(CONF_MIN_INTERVAL, default=DEFAULT_MIN_INTERVAL): _number(
                    0, 3600, 1, "s"
                ),
                vol.Required(CONF_HEARTBEAT_MINUTES, default=DEFAULT_HEARTBEAT_MINUTES): _number(
                    0, 1440, 1, "min"
                ),
                vol.Required(CONF_GRACE_MINUTES, default=DEFAULT_GRACE_MINUTES): _number(
                    0, 1440, 1, "min"
                ),
                vol.Required(CONF_FALLBACK, default=FALLBACK_INTERNAL): _select(
                    [FALLBACK_INTERNAL, FALLBACK_KEEP], key="fallback"
                ),
                vol.Required(CONF_REMOTE_OPTION, default=DEFAULT_REMOTE_OPTION): TextSelector(),
                vol.Required(CONF_LOCAL_OPTION, default=DEFAULT_LOCAL_OPTION): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="external",
            data_schema=self.add_suggested_values_to_schema(schema, previous),
        )

    async def async_step_external_trv(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        climate = self._room[CONF_CLIMATES][self._trv_index]
        if user_input is not None:
            trv = {
                CONF_TRV_CLIMATE: climate,
                CONF_TRV_NUMBER: user_input[CONF_TRV_NUMBER],
            }
            if user_input.get(CONF_TRV_SELECT):
                trv[CONF_TRV_SELECT] = user_input[CONF_TRV_SELECT]
            self._trvs.append(trv)
            self._trv_index += 1
            if self._trv_index < len(self._room[CONF_CLIMATES]):
                return await self.async_step_external_trv()
            self._external[CONF_TRVS] = self._trvs
            return self._finish()

        known = self._existing_trvs.get(climate)
        if known:
            suggested = {
                CONF_TRV_NUMBER: known[CONF_TRV_NUMBER],
                CONF_TRV_SELECT: known.get(CONF_TRV_SELECT),
            }
        else:
            number, select = _discover_trv_entities(self.hass, climate)
            suggested = {CONF_TRV_NUMBER: number, CONF_TRV_SELECT: select}
        schema = vol.Schema(
            {
                vol.Required(CONF_TRV_NUMBER): EntitySelector(
                    EntitySelectorConfig(domain="number")
                ),
                vol.Optional(CONF_TRV_SELECT): EntitySelector(
                    EntitySelectorConfig(domain="select")
                ),
            }
        )
        return self.async_show_form(
            step_id="external_trv",
            data_schema=self.add_suggested_values_to_schema(
                schema, {k: v for k, v in suggested.items() if v}
            ),
            description_placeholders={"climate": climate},
        )

    def _finish(self) -> SubentryFlowResult:
        data = dict(self._room)
        if self._external:
            data[CONF_EXTERNAL] = self._external
        if self.source == "reconfigure":
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=data[CONF_NAME],
                data=data,
            )
        return self.async_create_entry(title=data[CONF_NAME], data=data)


# --------------------------------------------------------------------------------
# Hub options flow: schedules, profiles, rules
# --------------------------------------------------------------------------------


class HubOptionsFlow(OptionsFlow):
    """Edit schedules, profiles and trigger rules of a hub."""

    def __init__(self) -> None:
        self._room_id: str | None = None
        self._profile: str | None = None
        self._rule_id: str | None = None

    @property
    def hub(self) -> HubRuntime:
        return self.config_entry.runtime_data

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.config_entry.options)

    def _update_options(self, **changes: Any) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, options={**self._options, **changes}
        )

    def _rooms(self) -> dict[str, str]:
        return {
            sid: sub.title
            for sid, sub in self.config_entry.subentries.items()
            if sub.subentry_type == SUBENTRY_ROOM
        }

    # -- main menu -------------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["schedule", "profiles", "rules", "finish"]
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)

    # -- schedule editor ---------------------------------------------------------

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        rooms = self._rooms()
        if not rooms:
            return self.async_abort(reason="no_rooms")
        if user_input is not None:
            self._room_id = user_input[CONF_ROOM]
            return await self.async_step_schedule_profile()
        schema = vol.Schema({vol.Required(CONF_ROOM, default=next(iter(rooms))): _labeled(rooms)})
        return self.async_show_form(step_id="schedule", data_schema=schema)

    async def async_step_schedule_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profiles = self.hub.profiles
        if user_input is not None:
            self._profile = user_input[CONF_PROFILE]
            return await self.async_step_schedule_day()
        schema = vol.Schema(
            {vol.Required(CONF_PROFILE, default=self._profile or profiles[0]): _select(profiles)}
        )
        return self.async_show_form(
            step_id="schedule_profile",
            data_schema=schema,
            description_placeholders={"room": self._rooms().get(self._room_id or "", "")},
        )

    def _week_summary(self) -> str:
        week = self.hub.get_week(self._room_id, self._profile)  # type: ignore[arg-type]
        lines = []
        for day in DAYS:
            parts = " · ".join(
                f"{format_minute(p.minute)} {format_temp(p.temp)}°" for p in week[day]
            )
            lines.append(f"**{day.capitalize()}**  {parts}")
        return "\n\n".join(lines)

    async def async_step_schedule_day(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="schedule_day",
            menu_options=[f"edit_day_{d}" for d in DAYS] + ["schedule_profile", "init"],
            description_placeholders={
                "room": self._rooms().get(self._room_id or "", ""),
                "profile": self._profile or "",
                "summary": self._week_summary(),
            },
        )

    async def _edit_day(self, day: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._room_id and self._profile
        week = self.hub.get_week(self._room_id, self._profile)
        if user_input is not None:
            try:
                points = parse_day(user_input[CONF_SCHEDULE_TEXT])
            except ScheduleError as err:
                errors[CONF_SCHEDULE_TEXT] = err.code
            else:
                for target in {day, *user_input.get(CONF_COPY_TO, [])}:
                    await self.hub.async_set_day(self._room_id, self._profile, target, points)
                return await self.async_step_schedule_day()
        current = user_input[CONF_SCHEDULE_TEXT] if user_input else format_day(week[day])
        schema = vol.Schema(
            {
                vol.Required(CONF_SCHEDULE_TEXT): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(CONF_COPY_TO): SelectSelector(
                    SelectSelectorConfig(
                        options=[d for d in DAYS if d != day],
                        multiple=True,
                        translation_key="days",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id=f"edit_day_{day}",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_SCHEDULE_TEXT: current}),
            errors=errors,
            description_placeholders={
                "room": self._rooms().get(self._room_id, ""),
                "profile": self._profile,
            },
        )

    async def async_step_edit_day_mon(self, user_input=None):
        return await self._edit_day("mon", user_input)

    async def async_step_edit_day_tue(self, user_input=None):
        return await self._edit_day("tue", user_input)

    async def async_step_edit_day_wed(self, user_input=None):
        return await self._edit_day("wed", user_input)

    async def async_step_edit_day_thu(self, user_input=None):
        return await self._edit_day("thu", user_input)

    async def async_step_edit_day_fri(self, user_input=None):
        return await self._edit_day("fri", user_input)

    async def async_step_edit_day_sat(self, user_input=None):
        return await self._edit_day("sat", user_input)

    async def async_step_edit_day_sun(self, user_input=None):
        return await self._edit_day("sun", user_input)

    # -- profiles -------------------------------------------------------------------

    async def async_step_profiles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="profiles",
            menu_options=["profile_add", "profile_rename", "profile_delete", "init"],
            description_placeholders={"profiles": ", ".join(self.hub.profiles)},
        )

    async def async_step_profile_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profiles = self.hub.profiles
        if user_input is not None:
            name = user_input[CONF_NEW_NAME].strip()
            if not name or len(name) > MAX_PROFILE_NAME:
                errors[CONF_NEW_NAME] = "invalid_profiles"
            elif name.casefold() in {p.casefold() for p in profiles}:
                errors[CONF_NEW_NAME] = "profile_exists"
            else:
                self.hub.add_profile_schedules(name, user_input.get(CONF_COPY_FROM))
                self._update_options(**{CONF_PROFILES: [*profiles, name]})
                return await self.async_step_profiles()
        schema = vol.Schema(
            {
                vol.Required(CONF_NEW_NAME): TextSelector(),
                vol.Optional(CONF_COPY_FROM): _select(profiles),
            }
        )
        return self.async_show_form(step_id="profile_add", data_schema=schema, errors=errors)

    async def async_step_profile_rename(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profiles = self.hub.profiles
        if user_input is not None:
            old, new = user_input[CONF_PROFILE], user_input[CONF_NEW_NAME].strip()
            others = {p.casefold() for p in profiles if p != old}
            if not new or len(new) > MAX_PROFILE_NAME:
                errors[CONF_NEW_NAME] = "invalid_profiles"
            elif new.casefold() in others:
                errors[CONF_NEW_NAME] = "profile_exists"
            else:
                self.hub.rename_profile_state(old, new)
                rules = [
                    {**r, CONF_RULE_PROFILE: new} if r[CONF_RULE_PROFILE] == old else r
                    for r in self.hub.rules
                ]
                self._update_options(
                    **{CONF_PROFILES: [new if p == old else p for p in profiles], CONF_RULES: rules}
                )
                return await self.async_step_profiles()
        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE): _select(profiles),
                vol.Required(CONF_NEW_NAME): TextSelector(),
            }
        )
        return self.async_show_form(step_id="profile_rename", data_schema=schema, errors=errors)

    async def async_step_profile_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profiles = self.hub.profiles
        if user_input is not None:
            name = user_input[CONF_PROFILE]
            if len(profiles) <= 1:
                errors[CONF_PROFILE] = "last_profile"
            elif any(r[CONF_RULE_PROFILE] == name for r in self.hub.rules):
                errors[CONF_PROFILE] = "profile_in_use"
            else:
                self._update_options(**{CONF_PROFILES: [p for p in profiles if p != name]})
                return await self.async_step_profiles()
        schema = vol.Schema({vol.Required(CONF_PROFILE): _select(profiles)})
        return self.async_show_form(step_id="profile_delete", data_schema=schema, errors=errors)

    # -- rules ------------------------------------------------------------------------

    def _rule_summary(self) -> str:
        rules = self.hub.rules
        if not rules:
            return "—"
        return "\n".join(
            f"- **{r[CONF_RULE_NAME]}** → {r[CONF_RULE_PROFILE]} (priority {r[CONF_RULE_PRIORITY]})"
            for r in rules
        )

    async def async_step_rules(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["rule_add"]
        if self.hub.rules:
            options += ["rule_edit", "rule_delete"]
        return self.async_show_menu(
            step_id="rules",
            menu_options=[*options, "init"],
            description_placeholders={"rules": self._rule_summary()},
        )

    def _rule_choices(self) -> dict[str, str]:
        return {r[CONF_RULE_ID]: r[CONF_RULE_NAME] for r in self.hub.rules}

    async def async_step_rule_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._rule_id = None
        return await self._rule_form("rule_add", user_input, {})

    async def async_step_rule_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._rule_id = user_input[CONF_RULE]
            rule = next(r for r in self.hub.rules if r[CONF_RULE_ID] == self._rule_id)
            return await self._rule_form("rule_edit_form", None, rule)
        schema = vol.Schema({vol.Required(CONF_RULE): _labeled(self._rule_choices())})
        return self.async_show_form(step_id="rule_edit", data_schema=schema)

    async def async_step_rule_edit_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._rule_form("rule_edit_form", user_input, {})

    async def async_step_rule_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._update_options(
                **{
                    CONF_RULES: [
                        r for r in self.hub.rules if r[CONF_RULE_ID] != user_input[CONF_RULE]
                    ]
                }
            )
            return await self.async_step_rules()
        schema = vol.Schema({vol.Required(CONF_RULE): _labeled(self._rule_choices())})
        return self.async_show_form(step_id="rule_delete", data_schema=schema)

    async def _rule_form(
        self, step_id: str, user_input: dict[str, Any] | None, defaults: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_RULE_ENTITIES]:
                errors[CONF_RULE_ENTITIES] = "no_entities"
            elif not user_input[CONF_RULE_STATE].strip():
                errors[CONF_RULE_STATE] = "no_state"
            else:
                rule = {
                    CONF_RULE_ID: self._rule_id or uuid.uuid4().hex[:8],
                    CONF_RULE_NAME: user_input[CONF_RULE_NAME].strip(),
                    CONF_RULE_SCOPE: user_input[CONF_RULE_SCOPE],
                    CONF_RULE_ENTITIES: user_input[CONF_RULE_ENTITIES],
                    CONF_RULE_MATCH: user_input[CONF_RULE_MATCH],
                    CONF_RULE_STATE: user_input[CONF_RULE_STATE].strip(),
                    CONF_RULE_FOR_MINUTES: float(user_input[CONF_RULE_FOR_MINUTES]),
                    CONF_RULE_PROFILE: user_input[CONF_RULE_PROFILE],
                    CONF_RULE_PRIORITY: int(user_input[CONF_RULE_PRIORITY]),
                }
                rules = [r for r in self.hub.rules if r[CONF_RULE_ID] != rule[CONF_RULE_ID]]
                position = next(
                    (
                        i
                        for i, r in enumerate(self.hub.rules)
                        if r[CONF_RULE_ID] == rule[CONF_RULE_ID]
                    ),
                    len(rules),
                )
                rules.insert(position, rule)
                self._update_options(**{CONF_RULES: rules})
                return await self.async_step_rules()
            defaults = user_input

        scopes = {SCOPE_ALL: "All rooms", **self._rooms()}
        schema = vol.Schema(
            {
                vol.Required(CONF_RULE_NAME): TextSelector(),
                vol.Required(CONF_RULE_SCOPE, default=SCOPE_ALL): _labeled(scopes),
                vol.Required(CONF_RULE_ENTITIES): EntitySelector(
                    EntitySelectorConfig(multiple=True)
                ),
                vol.Required(CONF_RULE_MATCH, default=MATCH_ANY): _select(
                    [MATCH_ANY, MATCH_ALL], key="match"
                ),
                vol.Required(CONF_RULE_STATE): TextSelector(),
                vol.Required(CONF_RULE_FOR_MINUTES, default=0): _number(0, 1440, 1, "min"),
                vol.Required(CONF_RULE_PROFILE): _select(self.hub.profiles),
                vol.Required(CONF_RULE_PRIORITY, default=10): _number(0, 1000, 1),
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors,
        )
