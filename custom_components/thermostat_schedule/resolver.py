"""Pure profile resolution: base profile + rules + pin -> effective profile."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from .const import CONF_RULE_ID, CONF_RULE_PRIORITY, CONF_RULE_PROFILE, CONF_RULE_SCOPE, SCOPE_ALL


@dataclass(frozen=True)
class Resolution:
    """The effective profile and why it was chosen."""

    profile: str
    reason: str  # "pinned" | "rule:<name>" | "base" | "fallback"
    rule_id: str | None = None


def resolve_profile(
    *,
    base_profile: str,
    rules: Collection[Mapping[str, Any]],
    active_rule_ids: Collection[str],
    room_id: str | None,
    pin: str | None,
    available_profiles: Collection[str],
    fallback_profile: str,
) -> Resolution:
    """Compute the effective profile.

    Order: room pin > highest-priority active rule (global or scoped to this room)
    > base profile. If the result has no schedule in ``available_profiles`` the
    fallback profile is used instead.
    """
    if pin is not None:
        chosen = Resolution(pin, "pinned")
    else:
        candidates = [
            rule
            for rule in rules
            if rule[CONF_RULE_ID] in active_rule_ids
            and rule.get(CONF_RULE_SCOPE, SCOPE_ALL) in (SCOPE_ALL, room_id)
        ]
        # max() keeps the first of equal priorities -> earlier rules win ties.
        winner = max(candidates, key=lambda r: r.get(CONF_RULE_PRIORITY, 0)) if candidates else None
        if winner is not None:
            chosen = Resolution(
                winner[CONF_RULE_PROFILE], f"rule:{winner.get('name', '')}", winner[CONF_RULE_ID]
            )
        else:
            chosen = Resolution(base_profile, "base")

    if chosen.profile in available_profiles:
        return chosen
    return Resolution(fallback_profile, "fallback")
