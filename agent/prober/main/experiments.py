"""Experiments: stacked-deck games that probe one behavior.

A deck is dealt round-robin (seat s gets deck[s::players] for its first
`hand_size` cards) and the rest is the draw pile, top first. So an experiment
states each seat's hand and the pile, and `build_deck` interleaves them.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ActionSpec(BaseModel):
    type: Literal["play", "draw", "pass"]
    card: str | None = None

    def as_action(self) -> dict:
        return {"type": self.type, "card": self.card} if self.type == "play" else {"type": self.type}


class Experiment(BaseModel):
    id: str
    title: str
    players: int = Field(ge=2, le=4)
    hands: list[list[str]]
    pile: list[str] = Field(default_factory=list)
    actions: list[ActionSpec] = Field(default_factory=list)   # played in order by whoever is up
    policy: Literal["draw_first", "play_first", "random"] | None = None
    max_steps: int = Field(default=80, ge=1, le=400)
    claim_topics: list[str] = Field(default_factory=list)
    rationale: str = ""
    split: Literal["train", "heldout"] = "train"


def build_deck(exp: Experiment, hand_size: int) -> list[str]:
    deck: list[str] = []
    for position in range(hand_size):
        for seat in range(exp.players):
            deck.append(exp.hands[seat][position])
    return deck + list(exp.pile)


def validate_experiment(exp: Experiment, hand_size: int, known_cards: set[str]) -> list[str]:
    errors = []
    if len(exp.hands) != exp.players:
        errors.append(f"needs {exp.players} hands, got {len(exp.hands)}")
    for seat, hand in enumerate(exp.hands):
        if len(hand) != hand_size:
            errors.append(f"seat {seat} hand has {len(hand)} cards, must be {hand_size}")
    used = {c for h in exp.hands for c in h} | set(exp.pile) | {a.card for a in exp.actions if a.card}
    unknown = sorted(c for c in used if c not in known_cards)
    if unknown:
        errors.append(f"unknown cards {unknown}")
    for spec in exp.actions:
        if (spec.type == "play") != (spec.card is not None):
            errors.append("a play needs a card and draw/pass must not have one")
    if not exp.actions and not exp.policy:
        errors.append("give either actions or a policy")
    return errors


def _hand(first: list[str], hand_size: int, filler: str = "vent") -> list[str]:
    return (first + [filler] * hand_size)[:hand_size]


def _plays(*cards: str) -> list[ActionSpec]:
    return [ActionSpec(type="play", card=c) for c in cards]


def seed_experiments(hand_size: int = 4) -> list[Experiment]:
    """Experiments aimed at the lost functions and unknown parameters (T5.5)."""
    h = lambda cards, filler="vent": _hand(cards, hand_size, filler)
    return [
        Experiment(
            id="e-draw-limit", title="Draw until the hand is full",
            players=2, hands=[h([], "scan"), h([], "scan")], pile=["vent", "scan"] * 20,
            policy="draw_first", max_steps=60, claim_topics=["function:_draw", "param:hand_limit"],
            rationale="Both seats draw whenever allowed; where draw disappears is the hand limit."),
        Experiment(
            id="e-jam-draw", title="Jam a seat, then make it draw and play",
            players=2, hands=[h(["jam"]), h(["scan", "gear1"])], pile=["vent"] * 12,
            actions=[*_plays("jam"), ActionSpec(type="draw"), *_plays("vent", "scan", "vent", "gear1")],
            claim_topics=["function:_jam", "function:_draw"],
            rationale="Does drawing use up or keep the jam? Is the next played card wasted?"),
        Experiment(
            id="e-double-twice", title="Double, then double again",
            players=2, hands=[h(["double"]), h(["double", "double"])], pile=["vent"] * 12,
            actions=_plays("double", "double", "double", "vent", "vent", "vent", "vent"),
            claim_topics=["function:_double"],
            rationale="Does a second double stack to 3 actions or replace the first?"),
        Experiment(
            id="e-reverse-3p", title="Reverse with 3 players",
            players=3, hands=[h(["reverse"]), h([]), h([])], pile=["vent"] * 12,
            actions=_plays("reverse", *["vent"] * 8), claim_topics=["function:_reverse"],
            rationale="Record who plays after a reverse; a skip would also show up here."),
        Experiment(
            id="e-reverse-4p", title="Reverse with 4 players",
            players=4, hands=[h(["reverse", "reverse"]), h([]), h([]), h([])], pile=["vent"] * 12,
            actions=_plays("reverse", *["vent"] * 4, "reverse", *["vent"] * 5),
            claim_topics=["function:_reverse"],
            rationale="Two reverses in 4 players: direction flips back."),
        Experiment(
            id="e-gear-arith", title="Each gear's pressure change",
            players=2, hands=[h(["gear1", "gear2", "gear3", "vent"]), h([], "scan")],
            pile=["vent"] * 12,
            actions=_plays("gear1", "scan", "gear2", "scan", "gear3", "scan", "vent"),
            claim_topics=["function:_gear", "function:_vent"],
            rationale="Isolate gear1/2/3 and vent arithmetic from other effects."),
        Experiment(
            id="e-overflow-3p", title="Overflow with 3 players",
            players=3, hands=[h(["gear3"] * 4), h([], "scan"), h([], "scan")], pile=["vent"] * 12,
            actions=_plays(*(["gear3", "scan", "scan"] * 3 + ["gear3"] + ["scan"] * 6)),
            claim_topics=["function:_gear", "overflow"],
            rationale="Push pressure to the limit; observe the elimination and who plays next."),
        Experiment(
            id="e-overflow-2p", title="Overflow with 2 players",
            players=2, hands=[h(["gear3"] * 4), h([], "scan")], pile=["vent"] * 12,
            actions=_plays(*(["gear3", "scan"] * 3 + ["gear3"])),
            claim_topics=["function:_gear", "overflow", "ending"],
            rationale="Overflow in a 2-player game ends the game; check the winner."),
        Experiment(
            id="e-vent-floor", title="Vent at and near zero",
            players=2, hands=[h(["vent", "gear1", "vent", "vent"]), h([], "scan")],
            pile=["vent"] * 12,
            actions=_plays("vent", "scan", "gear1", "scan", "vent", "scan", "vent"),
            claim_topics=["function:_vent"], rationale="Does pressure floor at 0?"),
        Experiment(
            id="e-stalemate", title="Everyone runs out of cards",
            players=2, hands=[h(["scan", "vent", "scan", "vent"]), h(["scan", "vent", "scan", "vent"])],
            pile=[], actions=_plays("scan", "scan", "vent", "vent", "scan", "scan", "vent", "vent"),
            claim_topics=["ending:stalemate"],
            rationale="Empty pile and empty hands: what does game_over report?"),
        Experiment(
            id="e-scan-short", title="Scan with a short pile",
            players=2, hands=[h(["scan"]), h([])], pile=["vent", "gear1"],
            actions=_plays("scan"), claim_topics=["param:scan_depth"],
            rationale="How many cards does scan show when only 2 remain?"),
    ]
