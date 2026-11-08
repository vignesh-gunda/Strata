"""T12: controls that re-score a saved run's rules. No model is called here.

The rules a run proposed are fixed. What changes between controls is the evidence they are
scored on (how many probe games, in what order, with which names for the cards). The
Scorer, which alone decides Confirmed and Refuted, does the deciding each time.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any

from auditor.main.corpus import GameRecord, TraceStore
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from scorer.main.scorer import Scorer


@dataclass(frozen=True)
class RuleSpec:
    key: str                 # digest of the code: the same rule has the same key in every control
    group: str
    kind: str
    covers: tuple[str, ...]
    code: str
    text: str


def rule_key(code: str) -> str:
    return hashlib.sha1(code.encode()).hexdigest()[:12]


def rules_from_ledger(ledger: Ledger) -> list[RuleSpec]:
    """Every executable rule the run proposed, whatever its status was at the end."""
    specs = []
    for c in ledger.all_claims():
        r = c.rule or {}
        if c.source is Source.MODEL and r.get("type") == "executable":
            specs.append(RuleSpec(rule_key(r["code"]), r["group"], r["kind"], tuple(r["covers"]), r["code"], c.text))
    return specs


@dataclass
class Outcome:
    games: int
    probe_games: int
    probe_steps: int
    statuses: dict[str, str] = field(default_factory=dict)         # rule key -> status
    heldout_correct: int = 0
    heldout_applicable: int = 0

    def keys(self, status: str) -> set[str]:
        return {k for k, s in self.statuses.items() if s == status}

    @property
    def groups_with_confirmed(self) -> int:
        return len(self._confirmed_groups)

    _confirmed_groups: set[str] = field(default_factory=set, repr=False)

    @property
    def heldout_accuracy(self) -> float | None:
        return self.heldout_correct / self.heldout_applicable if self.heldout_applicable else None

    def summary(self) -> dict[str, Any]:
        counts = {s: len(self.keys(s)) for s in ("confirmed", "inferred", "refuted", "open")}
        return {"games": self.games, "probe_games": self.probe_games, "probe_steps": self.probe_steps,
                **counts, "groups_with_confirmed": self.groups_with_confirmed,
                "heldout_accuracy": None if self.heldout_accuracy is None else round(self.heldout_accuracy, 4)}


def rescore(rules: list[RuleSpec], games: list[GameRecord]) -> Outcome:
    """Score every rule on `games` from scratch, the way the end of a run does (after separation)."""
    store = TraceStore(copy.deepcopy(games))
    ledger = Ledger()
    ids = {}
    for spec in rules:
        claim = ledger.add_claim(text=spec.text, source=Source.MODEL, created_by="controls", key=spec.key,
                                 rule={"type": "executable", "group": spec.group, "kind": spec.kind,
                                       "covers": list(spec.covers), "code": spec.code})
        ids[claim.id] = spec
    scorer = Scorer(ledger, store)
    for group in sorted({s.group for s in rules}):
        scorer.adjudicate_group(group, collapse_equivalent=True)
    out = Outcome(games=len(games), probe_games=sum(g.origin == "probe" for g in games),
                  probe_steps=sum(len(g.steps) for g in games if g.origin == "probe"))
    for claim in ledger.all_claims():
        spec = ids[claim.id]
        out.statuses[spec.key] = claim.status.value
        if claim.status is Status.CONFIRMED:
            out._confirmed_groups.add(spec.group)
        if claim.status is not Status.REFUTED:
            score = scorer.score_rule(spec.code)
            out.heldout_correct += score.correct["heldout"]
            out.heldout_applicable += score.applicable["heldout"]
    return out
