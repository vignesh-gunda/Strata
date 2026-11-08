"""Seat policies that make the simulated games diverse.

A policy picks one action from the legal list. Named policies rely on plain
card names; opaque runs use ``random`` only.
"""

from __future__ import annotations

import random
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

Policy = Callable[[Sequence[dict], dict, random.Random], dict]

SPECIALS = ("double", "reverse", "jam", "scan")
GEARS = ("gear3", "gear2", "gear1")


def _cards(legal: Sequence[dict], names: Sequence[str]) -> list[dict]:
    return [action for action in legal if action.get("card") in names]


def random_policy(legal: Sequence[dict], view: dict, rng: random.Random) -> dict:
    return rng.choice(list(legal))


def aggressive(legal: Sequence[dict], view: dict, rng: random.Random) -> dict:
    """Push pressure up: play the biggest gear available."""
    for gear in GEARS:
        found = _cards(legal, [gear])
        if found:
            return found[0]
    return random_policy(legal, view, rng)


def cautious(legal: Sequence[dict], view: dict, rng: random.Random) -> dict:
    """Vent when pressure is high, otherwise avoid gears and prefer drawing."""
    vents = _cards(legal, ["vent"])
    if view["pressure"] >= 6 and vents:
        return vents[0]
    safe = [action for action in legal if action.get("card") not in GEARS]
    return rng.choice(safe or list(legal))


def tactician(legal: Sequence[dict], view: dict, rng: random.Random) -> dict:
    """Play special cards first, to exercise turn-flow interactions."""
    specials = _cards(legal, SPECIALS)
    return rng.choice(specials) if specials else random_policy(legal, view, rng)


POLICIES: Mapping[str, Policy] = MappingProxyType(
    {"random": random_policy, "aggressive": aggressive, "cautious": cautious, "tactician": tactician}
)
