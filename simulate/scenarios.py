"""Targeted situations: stacked decks plus a scripted opening.

Each template deals chosen cards to chosen seats and then forces an opening
sequence of actions, so rare rule interactions show up in the traces.
After the opening, a normal policy plays the rest of the game.
Card names are the plain ones, so templates are skipped in opaque runs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

HAND_SIZE = 4


@dataclass(frozen=True)
class Template:
    players: int
    hands: Mapping[int, tuple[str, ...]]
    opening: tuple[dict, ...]
    pile_top: tuple[str, ...] = ()


def _play(card: str) -> dict:
    return {"type": "play", "card": card}


DRAW = {"type": "draw"}


def _template(players, hands, opening, pile_top=()) -> Template:
    frozen_hands = MappingProxyType({seat: tuple(cards) for seat, cards in hands.items()})
    return Template(players, frozen_hands, tuple(opening), tuple(pile_top))


TEMPLATES: Mapping[str, Template] = MappingProxyType(
    {
        "jam_then_play": _template(2, {0: ["jam"], 1: ["gear3"]}, [_play("jam"), _play("gear3")]),
        "jam_then_draw": _template(2, {0: ["jam"]}, [_play("jam"), DRAW]),
        "jam_three_players": _template(3, {0: ["jam"], 1: ["gear2"], 2: ["gear2"]},
                                       [_play("jam"), _play("gear2"), _play("gear2")]),
        "double_once": _template(3, {0: ["double"]}, [_play("double")]),
        "double_chain": _template(3, {0: ["double"], 1: ["double", "double"]},
                                  [_play("double"), _play("double"), _play("double")]),
        "reverse_three": _template(3, {0: ["reverse"]}, [_play("reverse")]),
        "reverse_four": _template(4, {0: ["reverse"], 3: ["gear1"]}, [_play("reverse"), _play("gear1")]),
        "double_reverse_jam": _template(3, {0: ["double"], 1: ["reverse", "jam"]},
                                        [_play("double"), _play("reverse"), _play("jam")]),
        "overflow": _template(3, {0: ["gear3", "gear3"], 1: ["gear3", "gear3"], 2: ["gear2"]},
                              [_play("gear3"), _play("gear3"), _play("gear2"), _play("gear3"), _play("gear3")]),
        "vent_from_five": _template(2, {0: ["gear3", "vent"], 1: ["gear2"]},
                                    [_play("gear3"), _play("gear2"), _play("vent")]),
        "scan_top": _template(2, {0: ["scan"]}, [_play("scan")], pile_top=["gear1", "jam", "vent"]),
        "draw_to_full": _template(2, {}, [DRAW, DRAW, DRAW, DRAW]),
    }
)


def arrange(
    deck: Sequence[str],
    players: int,
    hands: Mapping[int, Sequence[str]],
    pile_top: Sequence[str],
) -> list[str]:
    """Reorder ``deck`` so seats are dealt ``hands`` first and ``pile_top`` sits on the pile.

    The deck keeps exactly the same cards; only their order changes.
    """
    if any(len(cards) > HAND_SIZE for cards in hands.values()):
        raise ValueError(f"a seat can be given at most {HAND_SIZE} cards")
    if any(not 0 <= seat < players for seat in hands):
        raise ValueError("seat out of range")
    fixed = {index * players + seat: card for seat, cards in hands.items() for index, card in enumerate(cards)}
    fixed |= {players * HAND_SIZE + offset: card for offset, card in enumerate(pile_top)}
    pool = Counter(deck) - Counter(fixed.values())
    if Counter(fixed.values()) - Counter(deck):
        raise ValueError("the deck does not hold the requested cards")
    filler = iter(card for card in deck if pool[card] > 0 and not pool.subtract([card]))
    return [fixed[index] if index in fixed else next(filler) for index in range(len(deck))]
