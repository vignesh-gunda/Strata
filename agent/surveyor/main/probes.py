"""Rank what to investigate. Lost code and unknown parameters come first."""
from __future__ import annotations

from typing import Any

from ledger.main.models import Claim, Status
from ledger.main.store import Ledger

CLASSES = ("missing_function", "unknown_parameter", "hint_only", "documented_or_code")

_HINT_SUBKINDS = {"hint_not_in_manual", "hidden_state", "missing_module", "rule_variation"}

# Where each lost function or parameter should be tested, per the claims that talk about it.
_RELATED_TOPICS = {
    "_draw": ["action:draw"], "hand_limit": ["action:draw"],
    "scan_depth": ["card:scan"], "hand_size": ["setup:deal"],
}

_HINTS = {
    "_draw": "Fill a hand until draw stops being offered; jam a seat and make it draw; draw from "
             "a near-empty pile.",
    "_double": "Play double twice in a row: does the next seat get 2 actions or 3?",
    "_reverse": "Play reverse in 2-, 3- and 4-player games and record who plays next.",
    "_gear": "Raise pressure to and past the limit with each gear; what happens to pressure, the "
             "hand and the turn after an elimination?",
    "_vent": "Play vent at pressure 0, 1, 3 and 10 to see the change and any floor.",
    "_jam": "Jam a seat, then have it play each card kind and draw; does the jam clear?",
    "hand_limit": "Draw until the draw action disappears and record the hand size.",
    "scan_depth": "Play scan with a long and with a short draw pile.",
    "hand_size": "Read the opening views of 2-, 3- and 4-player games.",
    "stalemate": "Empty every hand and the draw pile and see what game_over reports.",
}
_GENERIC = {
    "hidden_state": "Design a game where this hidden value changes who plays next, and compare "
                    "against the visible turn order.",
    "missing_module": "Its contents decide the parameters; probe the behaviors it defines.",
    "rule_variation": "Run the same stacked-deck script under several program runs and compare.",
    "hint_not_in_manual": "The manual never mentions this; build a game that forces it.",
    None: "Check against the seed corpus first; probe only if the corpus cannot decide.",
}


def priority_class(claim: Claim) -> int:
    if claim.subkind == "missing_function":
        return 0
    if claim.subkind == "parameter":
        return 1
    if claim.subkind in _HINT_SUBKINDS:
        return 2
    return 3


def _subject(claim: Claim) -> str | None:
    if claim.subkind == "missing_function":
        return (claim.rule or {}).get("function")
    if claim.subkind == "parameter":
        return (claim.rule or {}).get("name")
    return None


def _related(claim: Claim, claims: list[Claim]) -> list[str]:
    subject = _subject(claim)
    if subject is None:
        return []
    topics = _RELATED_TOPICS.get(subject)
    if topics is None and subject.startswith("_"):
        topics = [f"card:{subject[1:]}"]
    topics = topics or []
    return [c.id for c in claims
            if c.kind == "behavior" and c.topic and any(c.topic.startswith(t) for t in topics)]


def _hint(claim: Claim) -> str:
    subject = _subject(claim)
    if subject in _HINTS:
        return _HINTS[subject]
    if claim.topic and claim.topic.startswith("ending:stalemate"):
        return _HINTS["stalemate"]
    return _GENERIC.get(claim.subkind, _GENERIC[None])


def _reason(claim: Claim, related: list[str]) -> str:
    if claim.subkind == "missing_function":
        return "Function body lost: the code cannot explain this behavior."
    if claim.subkind == "parameter":
        return "Parameter is read but its value is unknown."
    if claim.subkind in _HINT_SUBKINDS:
        return "Only the code hints at this; the manual does not cover it."
    return "Stated by an artifact; untested."


def build_probe_list(ledger: Ledger) -> list[dict[str, Any]]:
    claims = ledger.all_claims()
    candidates = [c for c in claims if c.status is Status.OPEN]

    def sort_key(c: Claim):
        return (priority_class(c), -len(_related(c, claims)), c.key or c.id)

    ranked = sorted(candidates, key=sort_key)
    items = []
    for rank, claim in enumerate(ranked, start=1):
        related = _related(claim, claims)
        cls = priority_class(claim)
        items.append({
            "rank": rank, "claim_id": claim.id, "claim_key": claim.key,
            "priority_class": cls, "class_name": CLASSES[cls],
            "kind": claim.kind, "subkind": claim.subkind, "topic": claim.topic,
            "source": claim.source.value, "text": claim.text,
            "reason": _reason(claim, related),
            "related_claim_ids": related,
            "experiment_hint": _hint(claim),
        })
    return items
