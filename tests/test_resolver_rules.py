"""Tests for profile resolution and rule conditions."""

from custom_components.thermostat_schedule.resolver import resolve_profile
from custom_components.thermostat_schedule.rules import condition_met

RULES = [
    {"id": "away", "name": "Nobody home", "profile": "away", "priority": 10, "scope": "all"},
    {"id": "vac", "name": "Vacation", "profile": "vacation", "priority": 20, "scope": "all"},
    {"id": "office", "name": "Office only", "profile": "away", "priority": 30, "scope": "room1"},
]
PROFILES = ["home", "away", "vacation"]


def resolve(active, *, room="room2", pin=None, base="home", profiles=PROFILES):
    return resolve_profile(
        base_profile=base,
        rules=RULES,
        active_rule_ids=active,
        room_id=room,
        pin=pin,
        available_profiles=profiles,
        fallback_profile="home",
    )


def test_base_when_no_rule_active() -> None:
    assert resolve(set()).profile == "home"


def test_returns_to_selected_base_after_rule_ends() -> None:
    assert resolve({"away"}, base="vacation").profile == "away"
    assert resolve(set(), base="vacation").profile == "vacation"


def test_highest_priority_wins() -> None:
    assert resolve({"away", "vac"}).profile == "vacation"


def test_room_scope() -> None:
    assert resolve({"office"}, room="room2").profile == "home"
    assert resolve({"office"}, room="room1").profile == "away"


def test_pin_beats_rules() -> None:
    res = resolve({"vac"}, pin="home")
    assert (res.profile, res.reason) == ("home", "pinned")


def test_fallback_when_profile_missing() -> None:
    res = resolve({"vac"}, profiles=["home", "away"])
    assert (res.profile, res.reason) == ("home", "fallback")


def test_condition_match_modes() -> None:
    rule = {"entities": ["person.a", "person.b"], "state": "not_home", "match": "all"}
    assert condition_met(rule, {"person.a": "not_home", "person.b": "not_home"})
    assert not condition_met(rule, {"person.a": "not_home", "person.b": "home"})
    assert condition_met({**rule, "match": "any"}, {"person.a": "not_home", "person.b": "home"})


def test_unavailable_never_satisfies() -> None:
    rule = {"entities": ["person.a"], "state": "not_home", "match": "any"}
    assert not condition_met(rule, {"person.a": "unavailable"})
    assert not condition_met(rule, {"person.a": None})


def test_is_not_with_several_states_covers_all_zones() -> None:
    rule = {
        "entities": ["person.a"],
        "operator": "is_not",
        "states": ["home"],
        "match": "any",
    }
    assert condition_met(rule, {"person.a": "not_home"})
    assert condition_met(rule, {"person.a": "Work"})  # any other zone counts as away
    assert not condition_met(rule, {"person.a": "home"})
    # unavailable never matches, not even "is not"
    assert not condition_met(rule, {"person.a": "unavailable"})
    assert not condition_met(rule, {"person.a": None})


def test_is_with_several_states() -> None:
    rule = {"entities": ["person.a"], "operator": "is", "states": ["home", "work"], "match": "any"}
    assert condition_met(rule, {"person.a": "work"})
    assert not condition_met(rule, {"person.a": "gym"})


def test_legacy_comma_separated_state() -> None:
    rule = {"entities": ["person.a"], "state": "home, work", "match": "any"}
    assert condition_met(rule, {"person.a": "work"})
