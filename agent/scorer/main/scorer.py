"""The Scorer: pure code, and the only writer of Confirmed and Refuted.

It holds the ledger's one-time scorer authority. Everything it decides comes from
traces: rules are run in a sandboxed worker and compared with recorded steps.

Confirmation needs ALL of:
  * zero mismatches on every split (train, probe, held-out)
  * at least `min_heldout` applicable held-out steps
  * a separating experiment: a probe step where it was right and a competitor in
    its group was wrong (or, with no competitor, a probe step where it applied)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from auditor.main.corpus import GameRecord, ScoringCorpus, TraceStore
from ledger.main.models import Claim, Source, Status
from ledger.main.store import Ledger, LedgerError
from sandbox.main.rule_worker import RuleError, RuleWorker

from .compare import matches, matches_setup

BUCKETS = ("train", "probe", "heldout")
MAX_IDS = 5
_STEP_FIELDS = ("before", "action", "events", "legal", "view", "seat", "turn")


def bucket_of(game: GameRecord) -> str:
    if game.split == "heldout":
        return "heldout"
    return "probe" if game.origin == "probe" else "train"


@dataclass
class RuleScore:
    kind: str
    covers: list[str]
    applicable: dict[str, int] = field(default_factory=lambda: {b: 0 for b in BUCKETS})
    correct: dict[str, int] = field(default_factory=lambda: {b: 0 for b in BUCKETS})
    mismatch_ids: dict[str, list[str]] = field(default_factory=lambda: {b: [] for b in BUCKETS})
    applicable_ids: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    load_error: str | None = None
    _hash: Any = field(default_factory=hashlib.sha1, repr=False)

    def note_prediction(self, trace_id: str, prediction: Any) -> None:
        self._hash.update(f"{trace_id}|{json.dumps(prediction, sort_keys=True)}\n".encode())

    @property
    def signature(self) -> str:
        """Same signature = same prediction on every step it applied to."""
        return self._hash.hexdigest()

    @property
    def mismatches(self) -> int:
        return sum(len(v) for v in self.mismatch_ids.values())

    def all_mismatch_ids(self) -> list[str]:
        order = ("train", "probe", "heldout")
        return [i for b in order for i in self.mismatch_ids[b]]

    def total_applicable(self) -> int:
        return sum(self.applicable.values())


@dataclass
class GroupReport:
    group: str
    confirmed: list[str] = field(default_factory=list)
    refuted: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)       # never applicable, or failed to load
    needs_experiment: list[str] = field(default_factory=list)
    scores: dict[str, RuleScore] = field(default_factory=dict)


class Scorer:
    def __init__(self, ledger: Ledger, store: TraceStore, *, min_heldout: int = 3,
                 require_probe_separation: bool = True):
        self.ledger = ledger
        self.store = store
        self.min_heldout = min_heldout
        self.require_probe_separation = require_probe_separation
        self._authority = ledger.grant_scorer()
        self._cache: dict[tuple[str, int], RuleScore] = {}

    # ---------- the only status writers ----------
    def refute(self, claim_id: str, trace_ids: list[str], reason: str) -> None:
        if self.ledger.get(claim_id).status is Status.REFUTED:
            return
        self.ledger.set_status(claim_id, Status.REFUTED, trace_ids,
                               authority=self._authority, reason=reason)

    def _confirm(self, claim_id: str, trace_ids: list[str], reason: str) -> None:
        self.ledger.set_status(claim_id, Status.CONFIRMED, trace_ids,
                               authority=self._authority, reason=reason)

    # ---------- running a rule over traces ----------
    def score_rule(self, code: str, corpus: ScoringCorpus | None = None) -> RuleScore:
        corpus = corpus or self.store.scoring()
        version = sum(len(g.steps) for g in corpus.games())
        key = (hashlib.sha1(code.encode()).hexdigest(), version)
        if key in self._cache:
            return self._cache[key]
        score = self._score(code, corpus)
        self._cache[key] = score
        return score

    def _score(self, code: str, corpus: ScoringCorpus) -> RuleScore:
        worker = RuleWorker()
        try:
            try:
                meta = worker.load(code)
            except RuleError as error:
                score = RuleScore(kind="", covers=[])
                score.load_error = str(error)
                return score
            score = RuleScore(kind=meta["kind"], covers=meta["covers"])
            if meta["kind"] == "setup":
                self._score_setup(worker, score, corpus)
            else:
                self._score_steps(worker, score, corpus)
            return score
        finally:
            worker.close()

    def _score_steps(self, worker: RuleWorker, score: RuleScore, corpus: ScoringCorpus) -> None:
        for game in corpus.games():
            if not game.steps:
                continue
            payload = [{k: s[k] for k in _STEP_FIELDS if k in s} for s in game.steps]
            try:
                results = worker.evaluate_game(payload)
            except RuleError as error:
                score.errors.append(f"{game.game_id}: {error}")
                break                                    # timeout or crash: stop trusting it
            bucket = bucket_of(game)
            for step, result in zip(game.steps, results):
                trace_id = step["trace_id"]
                if "err" in result:
                    score.applicable[bucket] += 1
                    score.mismatch_ids[bucket].append(trace_id)
                    score.errors.append(f"{trace_id}: {result['err']}")
                    continue
                prediction = result["p"]
                if prediction is None:
                    continue
                score.applicable[bucket] += 1
                score.applicable_ids.add(trace_id)
                score.note_prediction(trace_id, prediction)
                if matches(score.kind, score.covers, prediction, step):
                    score.correct[bucket] += 1
                else:
                    score.mismatch_ids[bucket].append(trace_id)

    def _score_setup(self, worker: RuleWorker, score: RuleScore, corpus: ScoringCorpus) -> None:
        games = list(corpus.games())
        results = worker.evaluate_setup([{"players": g.players, "deck": g.script["deck"]}
                                         for g in games])
        for game, result in zip(games, results):
            trace_id = game.steps[0]["trace_id"] if game.steps else game.game_id
            bucket = bucket_of(game)
            if "err" in result:
                score.applicable[bucket] += 1
                score.mismatch_ids[bucket].append(trace_id)
                score.errors.append(f"{game.game_id}: {result['err']}")
            elif result["p"] is not None:
                score.applicable[bucket] += 1
                score.applicable_ids.add(trace_id)
                score.note_prediction(trace_id, result["p"])
                if matches_setup(result["p"], game.initial_view):
                    score.correct[bucket] += 1
                else:
                    score.mismatch_ids[bucket].append(trace_id)

    # ---------- deciding a group of competing rules ----------
    def group_claims(self, group: str) -> list[Claim]:
        return [c for c in self.ledger.all_claims()
                if c.source is Source.MODEL and c.rule and c.rule.get("type") == "executable"
                and c.rule.get("group") == group]

    def adjudicate_group(self, group: str, *, collapse_equivalent: bool = False) -> GroupReport:
        report = GroupReport(group=group)
        claims = self.group_claims(group)
        scores = {c.id: self.score_rule(c.rule["code"]) for c in claims}
        report.scores = scores

        for claim in claims:                     # 1. refute anything with a counterexample
            score = scores[claim.id]
            if score.load_error:
                report.unscored.append(claim.id)
                continue
            ids = score.all_mismatch_ids()
            if ids and claim.status is not Status.REFUTED:
                detail = score.errors[0] if score.errors else "prediction differs from the binary"
                self.refute(claim.id, ids[:MAX_IDS], f"{len(ids)} mismatching step(s): {detail}")
            if self.ledger.get(claim.id).status is Status.REFUTED:
                report.refuted.append(claim.id)

        survivors = [c for c in claims if scores[c.id].load_error is None
                     and not scores[c.id].mismatches
                     and self.ledger.get(c.id).status is not Status.REFUTED]

        # Survivors that predicted identically on every step are one hypothesis, not rivals.
        # Only after a separation attempt (collapse_equivalent) do we confirm one representative.
        twins: dict[str, str] = {}                      # twin claim id -> representative id
        if collapse_equivalent:
            seen: dict[str, str] = {}
            for claim in survivors:
                score = scores[claim.id]
                if score.total_applicable() == 0:
                    continue
                rep = seen.setdefault(score.signature, claim.id)
                if rep != claim.id:
                    twins[claim.id] = rep

        for claim in survivors:                  # 2. confirm, infer, or ask for an experiment
            score = scores[claim.id]
            if self.ledger.get(claim.id).status is Status.CONFIRMED:
                report.confirmed.append(claim.id)        # still no counterexample: stays
                continue
            if claim.id in twins:
                rep = twins[claim.id]
                if score.total_applicable():
                    self.ledger.set_status(claim.id, Status.INFERRED, sorted(score.applicable_ids)[:MAX_IDS],
                                           reason=f"equivalent to {rep} on every step")
                report.inferred.append(claim.id)
                continue
            if score.total_applicable() == 0:
                report.unscored.append(claim.id)
                continue
            train_ids = sorted(score.applicable_ids)[:MAX_IDS]
            heldout_ok = score.applicable["heldout"] >= self.min_heldout
            separating = self._separating_ids(claim.id, score, scores, set(twins))
            probe_sep = [i for i in separating if i.startswith("x-")]
            probe_tested = any(i.startswith("x-") for i in score.applicable_ids)
            separated = bool(separating) and (probe_tested or not self.require_probe_separation)
            if heldout_ok and separated:
                held = [i for i in score.applicable_ids if self.store.split_of(i) == "heldout"]
                evidence = sorted(held)[:3] + (probe_sep or separating)[:3]
                self._confirm(claim.id, evidence,
                              f"{score.applicable['heldout']} held-out steps correct; "
                              f"separating step(s) {(probe_sep or separating)[:3]}")
                for trace_id in (probe_sep or separating)[:3]:
                    found = self.store.find_step(trace_id)
                    if found and found[0].meta.get("experiment_id"):
                        self.ledger.link_experiment(claim.id, found[0].meta["experiment_id"])
                report.confirmed.append(claim.id)
            else:
                self.ledger.set_status(claim.id, Status.INFERRED, train_ids,
                                       reason="fits the traces; not yet separated from alternatives")
                report.inferred.append(claim.id)
                report.needs_experiment.append(claim.id)
        return report

    def _separating_ids(self, claim_id: str, score: RuleScore, scores: dict[str, RuleScore],
                        ignore: set[str]) -> list[str]:
        """Steps where this rule applied and was right while a competitor was wrong.

        With no competitor at all, the separating steps are the probe steps it applied to.
        """
        competitors = [s for cid, s in scores.items()
                       if cid != claim_id and cid not in ignore and s.load_error is None]
        if not competitors:
            return sorted(i for i in score.applicable_ids if i.startswith("x-"))
        wrong_elsewhere: set[str] = set()
        for other in competitors:
            wrong_elsewhere.update(other.all_mismatch_ids())
        return sorted(score.applicable_ids & wrong_elsewhere)

    def explain(self, code: str, trace_ids: list[str], limit: int = 3) -> list[dict[str, Any]]:
        """For feedback: what the rule predicted vs what happened at some mismatching steps."""
        from auditor.main.digest import step_line
        from .compare import summarize_actual
        out: list[dict[str, Any]] = []
        worker = RuleWorker()
        try:
            meta = worker.load(code)
            wanted = list(trace_ids)[:limit]
            for trace_id in wanted:
                found = self.store.find_step(trace_id)
                if found is None and meta["kind"] != "setup":
                    continue
                if meta["kind"] == "setup":
                    for game in self.store.games():
                        if game.steps and game.steps[0]["trace_id"] == trace_id:
                            (res,) = worker.evaluate_setup([{"players": game.players,
                                                             "deck": game.script["deck"]}])
                            out.append({"trace_id": trace_id, "game": game.game_id,
                                        "expected": game.initial_view,
                                        "predicted": res.get("p", res.get("err"))})
                    continue
                game, step = found
                payload = [{k: s[k] for k in _STEP_FIELDS if k in s} for s in game.steps]
                results = worker.evaluate_game(payload)
                index = game.steps.index(step)
                res = results[index]
                out.append({"trace_id": trace_id, "step": step_line(step),
                            "expected": summarize_actual(meta["kind"], meta["covers"], step),
                            "predicted": res.get("p", res.get("err"))})
        except RuleError as error:
            out.append({"error": str(error)})
        finally:
            worker.close()
        return out
