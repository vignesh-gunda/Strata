"""Scripted randomness: every game is driven by an explicit deck order.

The original and the rebuild read the same script, so results can be
compared exactly. Probers can stack a deck to set up an experiment.
"""

from __future__ import annotations

import random
from typing import Sequence

from blackbox.cards import parse_card, standard_deck
from blackbox.rules import RuleSet

HAND_SIZE = RuleSet().hand_size


def script_from_seed(seed: int) -> dict:
    """A shuffled standard deck, reproducible from ``seed``."""
    order = random.Random(seed).sample(standard_deck(), len(standard_deck()))
    return {"deck": [card.value for card in order]}


def stacked_script(hands: Sequence[Sequence[str]], pile: Sequence[str]) -> dict:
    """Build a deck that deals exactly ``hands`` and then leaves ``pile`` on top."""
    if not 2 <= len(hands) <= 4:
        raise ValueError(f"need 2-4 hands, got {len(hands)}")
    if any(len(hand) != HAND_SIZE for hand in hands):
        raise ValueError(f"every hand must hold exactly {HAND_SIZE} cards")
    for name in [*(name for hand in hands for name in hand), *pile]:
        parse_card(name)
    dealt = [hand[index] for index in range(HAND_SIZE) for hand in hands]
    return {"deck": [*dealt, *pile]}
