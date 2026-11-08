"""Card catalogue for the Strata black box.

The black box is an original push-your-luck card game written for this
project ("Pressure"). Agents must never import this package; they reach the
game only through ``blackbox.client`` or ``blackbox.server``.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Card(str, Enum):
    GEAR1 = "gear1"
    GEAR2 = "gear2"
    GEAR3 = "gear3"
    VENT = "vent"
    REVERSE = "reverse"
    DOUBLE = "double"
    JAM = "jam"
    SCAN = "scan"


CARD_ORDER: tuple[Card, ...] = tuple(Card)

GEAR_HEAT: Mapping[Card, int] = MappingProxyType({Card.GEAR1: 1, Card.GEAR2: 2, Card.GEAR3: 3})

DECK_COMPOSITION: Mapping[Card, int] = MappingProxyType(
    {
        Card.GEAR1: 8,
        Card.GEAR2: 8,
        Card.GEAR3: 6,
        Card.VENT: 6,
        Card.REVERSE: 4,
        Card.DOUBLE: 4,
        Card.JAM: 3,
        Card.SCAN: 3,
    }
)


def standard_deck() -> tuple[Card, ...]:
    """The full 42-card deck in catalogue order (unshuffled)."""
    return tuple(card for card in CARD_ORDER for _ in range(DECK_COMPOSITION[card]))


def parse_card(name: str) -> Card:
    try:
        return Card(name)
    except ValueError as error:
        raise ValueError(f"unknown card: {name!r}") from error
