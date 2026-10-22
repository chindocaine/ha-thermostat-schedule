"""Constants for Thermostat Schedule."""

from __future__ import annotations

DOMAIN = "thermostat_schedule"

SUBENTRY_ROOM = "room"

# Weekdays, matching datetime.weekday() (Monday == 0).
DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Hub config entry data / options
CONF_PROFILES = "profiles"
CONF_RULES = "rules"
CONF_BASE_PROFILE = "base_profile"

# Rule keys
CONF_RULE_ID = "id"
CONF_RULE_NAME = "name"
CONF_RULE_PROFILE = "profile"
CONF_RULE_PRIORITY = "priority"
CONF_RULE_SCOPE = "scope"
CONF_RULE_ENTITIES = "entities"
CONF_RULE_MATCH = "match"
CONF_RULE_STATE = "state"  # legacy single (comma separated) state
CONF_RULE_STATES = "states"
CONF_RULE_OPERATOR = "operator"
CONF_RULE_FOR_MINUTES = "for_minutes"

SCOPE_ALL = "all"
OP_IS = "is"
OP_IS_NOT = "is_not"
MATCH_ANY = "any"
MATCH_ALL = "all"

# Room (subentry) data keys
CONF_NAME = "name"
CONF_CLIMATES = "climate_entities"
CONF_SYNC = "sync_thermostats"
CONF_HOLD_POLICY = "hold_policy"
CONF_HOLD_MINUTES = "hold_minutes"
CONF_EXTERNAL = "external_sensor"
CONF_SENSOR = "sensor"
CONF_TRVS = "trvs"
CONF_TRV_CLIMATE = "climate"
CONF_TRV_NUMBER = "number"
CONF_TRV_SELECT = "select"
CONF_MIN_CHANGE = "min_change"
CONF_MIN_INTERVAL = "min_interval"
CONF_HEARTBEAT_MINUTES = "heartbeat_minutes"
CONF_GRACE_MINUTES = "grace_minutes"
CONF_FALLBACK = "fallback"
CONF_REMOTE_OPTION = "remote_option"
CONF_LOCAL_OPTION = "local_option"

HOLD_NEXT_SWITCH = "next_switch"
HOLD_DURATION = "duration"
HOLD_UNTIL_RESUMED = "until_resumed"
HOLD_IGNORE = "ignore"
HOLD_POLICIES = (HOLD_NEXT_SWITCH, HOLD_DURATION, HOLD_UNTIL_RESUMED, HOLD_IGNORE)

FALLBACK_INTERNAL = "internal"
FALLBACK_KEEP = "keep"

DEFAULT_HOLD_MINUTES = 120
DEFAULT_MIN_CHANGE = 0.1
DEFAULT_MIN_INTERVAL = 30
DEFAULT_HEARTBEAT_MINUTES = 15
DEFAULT_GRACE_MINUTES = 60

DEFAULT_PROFILES = ["home", "away", "vacation"]

# Defaults for the temperature sensor select options (Sonoff TRVZB via Zigbee2MQTT).
# Both are configurable per room for other thermostats.
DEFAULT_REMOTE_OPTION = "remote_temperature"
DEFAULT_LOCAL_OPTION = "local_temperature"

# Profile pin select: "follow the hub" option (internal value stored as None).
PIN_FOLLOW_HUB = "follow_hub"

# Temperature presets usable in schedules by name (per room).
PRESET_ECO = "eco"
PRESET_COMFORT = "comfort"
PRESETS = (PRESET_ECO, PRESET_COMFORT)
CONF_ECO_TEMP = "eco_temperature"
CONF_COMFORT_TEMP = "comfort_temperature"
DEFAULT_ECO_TEMP = 19.0
DEFAULT_COMFORT_TEMP = 21.5

# Schedule limits
MIN_TEMP = 4.0
MAX_TEMP = 35.0
MAX_POINTS_PER_DAY = 12

# Echo suppression / debounce
EXPECTED_WRITE_TTL = 120.0
EXTERNAL_DEBOUNCE = 2.0

# Store
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.{{entry_id}}"

# Services
SERVICE_SET_OVERRIDE = "set_override"
ATTR_TEMPERATURE = "temperature"
ATTR_DURATION = "duration"
