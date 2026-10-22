"""Config flow: hub setup, room subentries and the hub options flow."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
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
    CONF_COMFORT_TEMP,
    CONF_ECO_TEMP,
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
    CONF_RULE_OPERATOR,
    CONF_RULE_PRIORITY,
    CONF_RULE_PROFILE,
    CONF_RULE_STATES,
    CONF_RULES,
    CONF_SENSOR,
    CONF_SYNC,
    CONF_TRV_CLIMATE,
    CONF_TRV_NUMBER,
    CONF_TRV_SELECT,
    CONF_TRVS,
    DAYS,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
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
    OP_IS,
    OP_IS_NOT,
    SUBENTRY_ROOM,
)
from .hub import HubRuntime
from .models import (
    DaySchedule,
    ScheduleError,
    format_day,
    format_minute,
    format_temp,
    parse_day,
)
from .rules import rule_states

CONF_COPY_FROM = "copy_from"
CONF_NEW_NAME = "new_name"
CONF_PROFILE = "profile"
CONF_USE_EXTERNAL = "use_external"
CONF_SCHEDULE_TEXT = "schedule"
CONF_COPY_TO = "copy_to"
CONF_USE_DEFAULT = "use_default"
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


_STATE_SUGGESTIONS = ["home", "not_home", "on", "off"]


class _ScheduleEditorMixin:
    """Schedule steps shared by the hub (default schedules) and room (overrides) flows."""

    _MAIN_STEP: str = "init"
    _CAN_RESET: bool = False
    _profile: str | None = None

    # Hooks -------------------------------------------------------------------
    @property
    def hub(self) -> HubRuntime:
        raise NotImplementedError

    def _scope_name(self) -> str:
        raise NotImplementedError

    def _editor_day(self, profile: str, day: str) -> tuple[DaySchedule, bool]:
        """Return the effective day schedule and whether it is a room override."""
        raise NotImplementedError

    async def _editor_save(self, profile: str, day: str, points: DaySchedule | None) -> None:
        """Save a day (``None`` resets a room override to the hub default)."""
        raise NotImplementedError

    async def _editor_reset_profile(self, profile: str) -> None:
        raise NotImplementedError

    async def _go_main(self) -> Any:
        return await getattr(self, f"async_step_{self._MAIN_STEP}")()

    # Steps ---------------------------------------------------------------------
    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> Any:
        profiles = self.hub.profiles
        if user_input is not None:
            self._profile = user_input[CONF_PROFILE]
            return await self.async_step_schedule_day()
        schema = vol.Schema(
            {vol.Required(CONF_PROFILE, default=self._profile or profiles[0]): _select(profiles)}
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="schedule",
            data_schema=schema,
            description_placeholders={"scope": self._scope_name()},
        )

    def _week_summary(self) -> str:
        assert self._profile
        lines = []
        for day in DAYS:
            points, custom = self._editor_day(self._profile, day)
            parts = " · ".join(
                f"{format_minute(p.minute)} {format_temp(p.temp)}"
                + ("" if isinstance(p.temp, str) else "°")
                for p in points
            )
            mark = " ✎" if custom else ""
            lines.append(f"**{day.capitalize()}**{mark}  {parts}")
        return "\n\n".join(lines)

    async def async_step_schedule_day(self, user_input: dict[str, Any] | None = None) -> Any:
        options = [f"edit_day_{d}" for d in DAYS]
        if self._CAN_RESET:
            options.append("schedule_reset")
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="schedule_day",
            menu_options=[*options, "schedule", self._MAIN_STEP],
            description_placeholders={
                "scope": self._scope_name(),
                "profile": self._profile or "",
                "summary": self._week_summary(),
            },
        )

    async def async_step_schedule_reset(self, user_input: dict[str, Any] | None = None) -> Any:
        assert self._profile
        await self._editor_reset_profile(self._profile)
        return await self.async_step_schedule_day()

    async def _edit_day(self, day: str, user_input: dict[str, Any] | None) -> Any:
        errors: dict[str, str] = {}
        assert self._profile
        if user_input is not None:
            targets = {day, *user_input.get(CONF_COPY_TO, [])}
            if self._CAN_RESET and user_input.get(CONF_USE_DEFAULT):
                for target in targets:
                    await self._editor_save(self._profile, target, None)
                return await self.async_step_schedule_day()
            try:
                points = parse_day(user_input[CONF_SCHEDULE_TEXT])
            except ScheduleError as err:
                errors[CONF_SCHEDULE_TEXT] = err.code
            else:
                for target in targets:
                    await self._editor_save(self._profile, target, points)
                return await self.async_step_schedule_day()
        current = (
            user_input[CONF_SCHEDULE_TEXT]
            if user_input
            else format_day(self._editor_day(self._profile, day)[0])
        )
        fields: dict[Any, Any] = {
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
        if self._CAN_RESET:
            fields[vol.Optional(CONF_USE_DEFAULT, default=False)] = BooleanSelector()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=f"edit_day_{day}",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                vol.Schema(fields), {CONF_SCHEDULE_TEXT: current}
            ),
            errors=errors,
            description_placeholders={"scope": self._scope_name(), "profile": self._profile},
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


class _RuleEditorMixin:
    """Rule steps shared by the hub (global rules) and room (room rules) flows."""

    _MAIN_STEP: str = "init"
    _rule_id: str | None = None

    @property
    def hub(self) -> HubRuntime:
        raise NotImplementedError

    def _rules_get(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _rules_set(self, rules: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    def _rule_summary(self) -> str:
        rules = self._rules_get()
        if not rules:
            return "—"
        lines = []
        for r in rules:
            op = "≠" if r.get(CONF_RULE_OPERATOR) == OP_IS_NOT else "="
            states = ", ".join(rule_states(r))
            lines.append(
                f"- **{r[CONF_RULE_NAME]}**: {', '.join(r[CONF_RULE_ENTITIES])} {op} {states}"
                f" → {r[CONF_RULE_PROFILE]} (priority {r[CONF_RULE_PRIORITY]})"
            )
        return "\n".join(lines)

    def _rule_choices(self) -> dict[str, str]:
        return {r[CONF_RULE_ID]: r[CONF_RULE_NAME] for r in self._rules_get()}

    async def async_step_rules(self, user_input: dict[str, Any] | None = None) -> Any:
        options = ["rule_add"]
        if self._rules_get():
            options += ["rule_edit", "rule_delete"]
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="rules",
            menu_options=[*options, self._MAIN_STEP],
            description_placeholders={"rules": self._rule_summary()},
        )

    async def async_step_rule_add(self, user_input: dict[str, Any] | None = None) -> Any:
        self._rule_id = None
        return await self._rule_form("rule_add", user_input, {})

    async def async_step_rule_edit(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._rule_id = user_input[CONF_RULE]
            rule = next(r for r in self._rules_get() if r[CONF_RULE_ID] == self._rule_id)
            return await self._rule_form(
                "rule_edit_form",
                None,
                {
                    **rule,
                    CONF_RULE_STATES: rule_states(rule),
                    CONF_RULE_OPERATOR: rule.get(CONF_RULE_OPERATOR, OP_IS),
                },
            )
        schema = vol.Schema({vol.Required(CONF_RULE): _labeled(self._rule_choices())})
        return self.async_show_form(step_id="rule_edit", data_schema=schema)  # type: ignore[attr-defined]

    async def async_step_rule_edit_form(self, user_input: dict[str, Any] | None = None) -> Any:
        return await self._rule_form("rule_edit_form", user_input, {})

    async def async_step_rule_delete(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._rules_set(
                [r for r in self._rules_get() if r[CONF_RULE_ID] != user_input[CONF_RULE]]
            )
            return await self.async_step_rules()
        schema = vol.Schema({vol.Required(CONF_RULE): _labeled(self._rule_choices())})
        return self.async_show_form(step_id="rule_delete", data_schema=schema)  # type: ignore[attr-defined]

    async def _rule_form(
        self, step_id: str, user_input: dict[str, Any] | None, defaults: dict[str, Any]
    ) -> Any:
        errors: dict[str, str] = {}
        if user_input is not None:
            states = [s.strip() for s in user_input[CONF_RULE_STATES] if s.strip()]
            if not user_input[CONF_RULE_ENTITIES]:
                errors[CONF_RULE_ENTITIES] = "no_entities"
            elif not states:
                errors[CONF_RULE_STATES] = "no_state"
            else:
                existing = next(
                    (r for r in self._rules_get() if r[CONF_RULE_ID] == self._rule_id), {}
                )
                rule = {
                    k: v for k, v in existing.items() if k not in ("state", CONF_RULE_STATES)
                } | {
                    CONF_RULE_ID: self._rule_id or uuid.uuid4().hex[:8],
                    CONF_RULE_NAME: user_input[CONF_RULE_NAME].strip(),
                    CONF_RULE_ENTITIES: user_input[CONF_RULE_ENTITIES],
                    CONF_RULE_MATCH: user_input[CONF_RULE_MATCH],
                    CONF_RULE_OPERATOR: user_input[CONF_RULE_OPERATOR],
                    CONF_RULE_STATES: states,
                    CONF_RULE_FOR_MINUTES: float(user_input[CONF_RULE_FOR_MINUTES]),
                    CONF_RULE_PROFILE: user_input[CONF_RULE_PROFILE],
                    CONF_RULE_PRIORITY: int(user_input[CONF_RULE_PRIORITY]),
                }
                rules = self._rules_get()
                if existing:
                    rules = [rule if r[CONF_RULE_ID] == rule[CONF_RULE_ID] else r for r in rules]
                else:
                    rules.append(rule)
                self._rules_set(rules)
                return await self.async_step_rules()
            defaults = user_input

        schema = vol.Schema(
            {
                vol.Required(CONF_RULE_NAME): TextSelector(),
                vol.Required(CONF_RULE_ENTITIES): EntitySelector(
                    EntitySelectorConfig(multiple=True)
                ),
                vol.Required(CONF_RULE_MATCH, default=MATCH_ANY): _select(
                    [MATCH_ANY, MATCH_ALL], key="match"
                ),
                vol.Required(CONF_RULE_OPERATOR, default=OP_IS): _select(
                    [OP_IS, OP_IS_NOT], key="operator"
                ),
                vol.Required(CONF_RULE_STATES): SelectSelector(
                    SelectSelectorConfig(
                        options=_STATE_SUGGESTIONS,
                        multiple=True,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_RULE_FOR_MINUTES, default=0): _number(0, 1440, 1, "min"),
                vol.Required(CONF_RULE_PROFILE): _select(self.hub.profiles),
                vol.Required(CONF_RULE_PRIORITY, default=10): _number(0, 1000, 1),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, defaults),  # type: ignore[attr-defined]
            errors=errors,
        )


class RoomSubentryFlow(_ScheduleEditorMixin, _RuleEditorMixin, ConfigSubentryFlow):
    """Add a room; the room's page offers settings, schedule overrides and room rules."""

    _MAIN_STEP = "reconfigure"
    _CAN_RESET = True

    def __init__(self) -> None:
        self._room: dict[str, Any] = {}
        self._external: dict[str, Any] = {}
        self._trv_index = 0
        self._trvs: list[dict[str, str]] = []
        self._existing_trvs: dict[str, dict[str, str]] = {}
        self._existing_external: dict[str, Any] = {}

    # -- helpers ---------------------------------------------------------------

    @property
    def hub(self) -> HubRuntime:
        return self._get_entry().runtime_data

    @property
    def _room_id(self) -> str:
        return self._get_reconfigure_subentry().subentry_id

    def _scope_name(self) -> str:
        return self._get_reconfigure_subentry().title

    def _editor_day(self, profile: str, day: str) -> tuple[DaySchedule, bool]:
        return (
            self.hub.effective_week(self._room_id, profile)[day],
            self.hub.is_override(self._room_id, profile, day),
        )

    async def _editor_save(self, profile: str, day: str, points: DaySchedule | None) -> None:
        await self.hub.async_set_day(self._room_id, profile, day, points)

    async def _editor_reset_profile(self, profile: str) -> None:
        await self.hub.async_reset_room_profile(self._room_id, profile)

    def _rules_get(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._get_reconfigure_subentry().data.get(CONF_RULES, [])]

    def _rules_set(self, rules: list[dict[str, Any]]) -> None:
        subentry = self._get_reconfigure_subentry()
        self.hass.config_entries.async_update_subentry(
            self._get_entry(), subentry, data={**subentry.data, CONF_RULES: rules}
        )

    # -- entry points --------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_room_form("user", user_input, {})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["settings", "schedule", "rules", "finish"],
            description_placeholders={"room": self._scope_name()},
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return self.async_abort(reason="reconfigure_successful")

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        data = dict(self._get_reconfigure_subentry().data)
        self._existing_external = data.get(CONF_EXTERNAL) or {}
        self._existing_trvs = {
            t[CONF_TRV_CLIMATE]: t for t in self._existing_external.get(CONF_TRVS, [])
        }
        return await self._async_room_form(
            "settings", user_input, {**data, CONF_USE_EXTERNAL: bool(self._existing_external)}
        )

    # -- room settings steps ----------------------------------------------------

    def _climates_in_use(self) -> set[str]:
        own = self._room_id if self.source == SOURCE_RECONFIGURE else None
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
                    CONF_ECO_TEMP: float(user_input[CONF_ECO_TEMP]),
                    CONF_COMFORT_TEMP: float(user_input[CONF_COMFORT_TEMP]),
                    CONF_SYNC: user_input[CONF_SYNC],
                    CONF_HOLD_POLICY: user_input[CONF_HOLD_POLICY],
                    CONF_HOLD_MINUTES: int(user_input[CONF_HOLD_MINUTES]),
                }
                self._external = {}
                if user_input[CONF_USE_EXTERNAL]:
                    return await self.async_step_external()
                return await self._finish()
            defaults = user_input

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): TextSelector(),
                vol.Required(CONF_CLIMATES): EntitySelector(
                    EntitySelectorConfig(domain="climate", multiple=True)
                ),
                vol.Required(CONF_ECO_TEMP, default=DEFAULT_ECO_TEMP): _number(4, 35, 0.5, "°C"),
                vol.Required(CONF_COMFORT_TEMP, default=DEFAULT_COMFORT_TEMP): _number(
                    4, 35, 0.5, "°C"
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
            return await self._finish()

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

    async def _finish(self) -> SubentryFlowResult:
        data = dict(self._room)
        if self._external:
            data[CONF_EXTERNAL] = self._external
        if self.source == SOURCE_RECONFIGURE:
            subentry = self._get_reconfigure_subentry()
            kept = {k: v for k, v in subentry.data.items() if k not in (*data, CONF_EXTERNAL)}
            self.hass.config_entries.async_update_subentry(
                self._get_entry(), subentry, data={**kept, **data}, title=data[CONF_NAME]
            )
            return await self.async_step_reconfigure()
        return self.async_create_entry(title=data[CONF_NAME], data=data)


# --------------------------------------------------------------------------------
# Hub options flow: default schedules, profiles, global rules
# --------------------------------------------------------------------------------


class HubOptionsFlow(_ScheduleEditorMixin, _RuleEditorMixin, OptionsFlow):
    """Edit the hub's default schedules, profiles and global trigger rules."""

    _MAIN_STEP = "init"

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

    def _scope_name(self) -> str:
        return self.config_entry.title

    def _editor_day(self, profile: str, day: str) -> tuple[DaySchedule, bool]:
        return self.hub.default_week(profile)[day], False

    async def _editor_save(self, profile: str, day: str, points: DaySchedule | None) -> None:
        assert points is not None
        await self.hub.async_set_default_day(profile, day, points)

    def _rules_get(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.hub.rules]

    def _rules_set(self, rules: list[dict[str, Any]]) -> None:
        self._update_options(**{CONF_RULES: rules})

    # -- main menu -------------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["schedule", "profiles", "rules", "finish"]
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(data=self._options)

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
                self._rename_in_room_rules(old, new)
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

    def _room_rules_using(self, profile: str) -> bool:
        return any(
            rule[CONF_RULE_PROFILE] == profile
            for sub in self.config_entry.subentries.values()
            for rule in sub.data.get(CONF_RULES, [])
        )

    def _rename_in_room_rules(self, old: str, new: str) -> None:
        for sub in self.config_entry.subentries.values():
            rules = sub.data.get(CONF_RULES, [])
            if any(r[CONF_RULE_PROFILE] == old for r in rules):
                self.hass.config_entries.async_update_subentry(
                    self.config_entry,
                    sub,
                    data={
                        **sub.data,
                        CONF_RULES: [
                            {**r, CONF_RULE_PROFILE: new} if r[CONF_RULE_PROFILE] == old else r
                            for r in rules
                        ],
                    },
                )

    async def async_step_profile_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profiles = self.hub.profiles
        if user_input is not None:
            name = user_input[CONF_PROFILE]
            if len(profiles) <= 1:
                errors[CONF_PROFILE] = "last_profile"
            elif any(
                r[CONF_RULE_PROFILE] == name for r in self.hub.rules
            ) or self._room_rules_using(name):
                errors[CONF_PROFILE] = "profile_in_use"
            else:
                self._update_options(**{CONF_PROFILES: [p for p in profiles if p != name]})
                return await self.async_step_profiles()
        schema = vol.Schema({vol.Required(CONF_PROFILE): _select(profiles)})
        return self.async_show_form(step_id="profile_delete", data_schema=schema, errors=errors)
