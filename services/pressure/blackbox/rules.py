"""Rule set and hidden mutations for the Pressure black box.

The full rules are in ``sealed/RULES.md``. Agents never see this package.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Mutation(str, Enum):
    VENT_FOUR = "vent_four"  # vent lowers pressure by 4
    STACKING_DOUBLE = "stacking_double"  # each double adds one more action
    HOT_RESTART = "hot_restart"  # after an overflow pressure resets to 4
    JAM_BLOCKS_DRAW = "jam_blocks_draw"  # a jam also wastes a draw
    REVERSE_SKIPS = "reverse_skips"  # reverse also skips the next seat
    SHORT_SCAN = "short_scan"  # scan reveals only 1 card


MUTATION_ORDER: tuple[Mutation, ...] = tuple(Mutation)


@dataclass(frozen=True)
class RuleSet:
    hand_size: int = 4
    hand_limit: int = 6
    pressure_limit: int = 12
    vent_amount: int = 3
    overflow_reset: int = 0
    scan_depth: int = 2
    stacking_double: bool = False
    jam_blocks_draw: bool = False
    reverse_skips: bool = False
    mutations: tuple[Mutation, ...] = ()


def ruleset_for(mutations: Iterable[Mutation]) -> RuleSet:
    active = tuple(sorted(set(mutations), key=MUTATION_ORDER.index))
    return RuleSet(
        vent_amount=4 if Mutation.VENT_FOUR in active else 3,
        overflow_reset=4 if Mutation.HOT_RESTART in active else 0,
        scan_depth=1 if Mutation.SHORT_SCAN in active else 2,
        stacking_double=Mutation.STACKING_DOUBLE in active,
        jam_blocks_draw=Mutation.JAM_BLOCKS_DRAW in active,
        reverse_skips=Mutation.REVERSE_SKIPS in active,
        mutations=active,
    )


def pick_mutations(seed: int, count: int) -> tuple[Mutation, ...]:
    """Deterministically choose ``count`` distinct mutations for a run seed."""
    if not 0 <= count <= len(MUTATION_ORDER):
        raise ValueError(f"mutation count must be 0-{len(MUTATION_ORDER)}, got {count}")
    chosen = random.Random(seed).sample(MUTATION_ORDER, count)
    return tuple(sorted(chosen, key=MUTATION_ORDER.index))


def parse_mutation(name: str) -> Mutation:
    try:
        return Mutation(name)
    except ValueError as error:
        raise ValueError(f"unknown mutation: {name!r}") from error
