"""Tests for the pure schedule model."""

import pytest

from custom_components.thermostat_schedule.models import (
    ScheduleError,
    SwitchPoint,
    day_from_json,
    day_to_json,
    format_day,
    parse_day,
    resolve_temp,
)


def test_parse_basic() -> None:
    points = parse_day("00:00 17\n06:00 21.5\n22:00 17")
    assert points == [SwitchPoint(0, 17.0), SwitchPoint(360, 21.5), SwitchPoint(1320, 17.0)]


def test_parse_lenient_formats() -> None:
    points = parse_day("0:00=17,5\n6:30 - 21 °C\n\n  22:00;17.0  ")
    assert points == [SwitchPoint(0, 17.5), SwitchPoint(390, 21.0), SwitchPoint(1320, 17.0)]


def test_parse_inserts_midnight_with_last_temperature() -> None:
    points = parse_day("06:00 21\n22:00 17")
    assert points[0] == SwitchPoint(0, 17.0)
    assert len(points) == 3


def test_parse_sorts_input() -> None:
    assert parse_day("22:00 17\n00:00 18\n06:00 21")[1:] == [
        SwitchPoint(360, 21.0),
        SwitchPoint(1320, 17.0),
    ]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty_day"),
        ("hello", "invalid_format"),
        ("25:00 20", "invalid_time"),
        ("00:00 20\n00:00 21", "not_increasing"),
        ("00:00 3", "temp_range"),
        ("00:00 36", "temp_range"),
        ("\n".join(f"{h:02d}:00 20" for h in range(13)), "too_many_points"),
    ],
)
def test_parse_errors(text: str, code: str) -> None:
    with pytest.raises(ScheduleError) as err:
        parse_day(text)
    assert err.value.code == code


def test_roundtrip() -> None:
    points = parse_day("00:00 17\n06:30 21.5")
    assert format_day(points) == "00:00 17\n06:30 21.5"
    assert day_from_json(day_to_json(points)) == points


def test_presets_and_literal_temperatures_can_be_mixed() -> None:
    expected = [
        SwitchPoint(0, "eco"),
        SwitchPoint(480, "comfort"),
        SwitchPoint(1140, 23.0),
        SwitchPoint(1350, "eco"),
    ]
    assert parse_day("00:00 eco\n08:00 comfort\n19:00 23\n22:30 eco") == expected
    # the inline, comma separated notation works too (also with decimal commas)
    assert parse_day("00:00 eco, 08:00 comfort, 19:00 23, 22:30 eco") == expected
    assert parse_day("00:00 eco, 08:00 21,5")[1] == SwitchPoint(480, 21.5)
    assert parse_day("00:00 ECO\n08:00 Comfort") == [
        SwitchPoint(0, "eco"),
        SwitchPoint(480, "comfort"),
    ]


def test_preset_roundtrip_and_resolution() -> None:
    points = parse_day("00:00 eco, 08:00 comfort, 19:00 23")
    assert format_day(points) == "00:00 eco\n08:00 comfort\n19:00 23"
    assert day_from_json(day_to_json(points)) == points
    presets = {"eco": 17.0, "comfort": 22.5}
    assert [resolve_temp(p.temp, presets) for p in points] == [17.0, 22.5, 23.0]
    assert resolve_temp("eco") == 19.0  # defaults without room presets


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ScheduleError):
        parse_day("00:00 party")
