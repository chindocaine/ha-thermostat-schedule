"""Rule engine: evaluates trigger rules from entity states."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    CONF_RULE_ENTITIES,
    CONF_RULE_FOR_MINUTES,
    CONF_RULE_ID,
    CONF_RULE_MATCH,
    CONF_RULE_STATE,
    MATCH_ALL,
)


def condition_met(rule: Mapping[str, Any], states: Mapping[str, str | None]) -> bool:
    """Return whether a rule's raw condition holds (ignoring the ``for`` delay).

    Entities that do not exist or are unavailable/unknown never satisfy a rule, so a
    flaky tracker cannot trigger a switch to e.g. "away".
    """
    wanted = str(rule[CONF_RULE_STATE]).strip().lower()
    results = []
    for entity_id in rule[CONF_RULE_ENTITIES]:
        state = states.get(entity_id)
        if state is None or state in ("unavailable", "unknown"):
            results.append(False)
        else:
            results.append(state.strip().lower() == wanted)
    if not results:
        return False
    return all(results) if rule.get(CONF_RULE_MATCH) == MATCH_ALL else any(results)


class RuleEngine:
    """Tracks which rules are currently active and notifies on change."""

    def __init__(
        self,
        hass: HomeAssistant,
        rules: list[dict[str, Any]],
        on_change: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._rules = rules
        self._on_change = on_change
        self._active: set[str] = set()
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsub: CALLBACK_TYPE | None = None

    @property
    def active(self) -> frozenset[str]:
        """IDs of rules that are currently active."""
        return frozenset(self._active)

    @callback
    def async_start(self) -> None:
        """Evaluate once and start listening."""
        entities = sorted({e for r in self._rules for e in r[CONF_RULE_ENTITIES]})
        if entities:
            self._unsub = async_track_state_change_event(self._hass, entities, self._handle_event)
        for rule in self._rules:
            self._evaluate(rule, notify=False)

    @callback
    def async_stop(self) -> None:
        """Stop listening and cancel timers."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    def _states(self, rule: Mapping[str, Any]) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for entity_id in rule[CONF_RULE_ENTITIES]:
            state = self._hass.states.get(entity_id)
            result[entity_id] = state.state if state else None
        return result

    @callback
    def _handle_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        for rule in self._rules:
            if entity_id in rule[CONF_RULE_ENTITIES]:
                self._evaluate(rule, notify=True)

    def _evaluate(self, rule: dict[str, Any], *, notify: bool) -> None:
        rule_id = rule[CONF_RULE_ID]
        met = condition_met(rule, self._states(rule))
        if not met:
            self._cancel_timer(rule_id)
            if rule_id in self._active:
                self._active.discard(rule_id)
                if notify:
                    self._on_change()
            return
        if rule_id in self._active or rule_id in self._timers:
            return
        delay = float(rule.get(CONF_RULE_FOR_MINUTES) or 0) * 60
        if delay <= 0:
            self._active.add(rule_id)
            if notify:
                self._on_change()
            return

        @callback
        def _fire(_now: Any) -> None:
            self._timers.pop(rule_id, None)
            # Re-check: the condition might have flipped without us seeing a cancel.
            if condition_met(rule, self._states(rule)):
                self._active.add(rule_id)
                self._on_change()

        self._timers[rule_id] = async_call_later(self._hass, delay, _fire)

    def _cancel_timer(self, rule_id: str) -> None:
        cancel = self._timers.pop(rule_id, None)
        if cancel:
            cancel()
