"""What an observer may see, plus the opaque-token codec.

The view never exposes the draw-pile order, turn direction, jams, pending
extra actions, the pressure limit or the active mutations.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from blackbox.cards import CARD_ORDER, Card, parse_card
from blackbox.state import ACTION_KINDS, EVENT_TYPES, Action, GameState


def _identity(names) -> Mapping[str, str]:
    return MappingProxyType({name: name for name in names})


@dataclass(frozen=True)
class Codec:
    """Translates real card/action names to the names agents see, and back."""

    cards: Mapping[str, str] = field(default_factory=lambda: _identity(card.value for card in CARD_ORDER))
    actions: Mapping[str, str] = field(default_factory=lambda: _identity(ACTION_KINDS))
    events: Mapping[str, str] = field(default_factory=lambda: _identity(EVENT_TYPES))

    def encode_card(self, card: Card | str) -> str:
        return self.cards[Card(card).value]

    def decode_card(self, name: str) -> Card:
        reverse = {shown: real for real, shown in self.cards.items()}
        if name not in reverse:
            raise ValueError("unknown card name")
        return parse_card(reverse[name])

    def encode_action(self, action: Action) -> dict:
        encoded = {"type": self.actions[action.kind]}
        return encoded | ({"card": self.encode_card(action.card)} if action.card else {})

    def decode_action(self, kind: str, card: str | None) -> Action:
        reverse = {shown: real for real, shown in self.actions.items()}
        if kind not in reverse:
            raise ValueError("unknown action type")
        return Action(reverse[kind], self.decode_card(card) if card is not None else None)

    def encode_event(self, event: dict) -> dict:
        encoded = dict(event) | {"type": self.events[event["type"]]}
        if "card" in encoded:
            encoded["card"] = self.encode_card(encoded["card"])
        if "cards" in encoded:
            encoded["cards"] = [self.encode_card(name) for name in encoded["cards"]]
        return encoded

    def reveal(self) -> dict:
        return {"cards": dict(self.cards), "actions": dict(self.actions), "events": dict(self.events)}


def _tokens(rng: random.Random, names, prefix: str) -> Mapping[str, str]:
    shuffled = rng.sample(list(names), len(names))
    return MappingProxyType({name: f"{prefix}{index + 1}" for index, name in enumerate(shuffled)})


def opaque_codec(seed: int) -> Codec:
    """Rename cards to c1..cN, actions to a1..aK and event types to e1..eM."""
    rng = random.Random(seed)
    return Codec(
        cards=_tokens(rng, [card.value for card in CARD_ORDER], "c"),
        actions=_tokens(rng, ACTION_KINDS, "a"),
        events=_tokens(rng, EVENT_TYPES, "e"),
    )


def public_view(state: GameState, codec: Codec) -> dict:
    return {
        "players": state.players,
        "hands": [[codec.encode_card(card) for card in hand] for hand in state.hands],
        "alive": list(state.alive),
        "discard": [codec.encode_card(card) for card in state.discard],
        "draw_pile_size": len(state.draw_pile),
        "current_seat": state.current,
        "actions_left": state.actions_left,
        "pressure": state.pressure,
        "turn": state.turn,
        "over": state.over,
        "winner": state.winner,
    }
