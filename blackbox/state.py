"""Immutable game state and action types."""

from __future__ import annotations

from dataclasses import dataclass

from blackbox.cards import Card
from blackbox.rules import RuleSet

PLAY = "play"
DRAW = "draw"
PASS = "pass"
ACTION_KINDS: tuple[str, ...] = (PLAY, DRAW, PASS)
EVENT_TYPES: tuple[str, ...] = (
    "played",
    "pressure",
    "eliminated",
    "revealed",
    "drew",
    "recycled",
    "passed",
    "turn_passed",
    "game_over",
)


@dataclass(frozen=True)
class Action:
    kind: str
    card: Card | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError("unknown action type")
        if (self.kind == PLAY) != (self.card is not None):
            raise ValueError("invalid card argument for this action type")


@dataclass(frozen=True)
class GameState:
    rules: RuleSet
    hands: tuple[tuple[Card, ...], ...]
    alive: tuple[bool, ...]
    draw_pile: tuple[Card, ...]  # index 0 is the top card
    discard: tuple[Card, ...]
    jammed: tuple[bool, ...]
    pressure: int = 0
    current: int = 0
    direction: int = 1
    actions_left: int = 1
    extra_actions: int = 0  # granted to whoever takes the next turn
    skip_next: bool = False
    turn: int = 1
    over: bool = False
    winner: int | None = None

    @property
    def players(self) -> int:
        return len(self.hands)
