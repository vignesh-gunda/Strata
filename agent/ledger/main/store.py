"""The evidence ledger: an append-only event log with enforced transitions.

Rules enforced here, not by convention:
  * New claims always start OPEN. Nothing can be created Confirmed.
  * OBSERVED, INFERRED, CONFIRMED and REFUTED all need trace IDs.
  * CONFIRMED and REFUTED also need the scorer's authority token, which the
    ledger hands out exactly once. No model call ever holds it.
  * REFUTED is final. A claim is kept with its trace ID, never deleted.
  * A CONFIRMED claim can be demoted to INFERRED if it is not retested.
  * Predictions are append-only; outcomes are recorded separately.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import (TIER_FOR_SOURCE, Claim, Finding, FindingKind, Outcome,
                     Prediction, Source, Status, Tier, now)


class LedgerError(ValueError):
    """An illegal ledger operation."""


class ScorerAuthority:
    """Opaque token. Only the object returned by `Ledger.grant_scorer()` works."""
    __slots__ = ()


_ALLOWED = {
    Status.OPEN: {Status.OBSERVED, Status.INFERRED, Status.CONFIRMED, Status.REFUTED},
    Status.OBSERVED: {Status.OBSERVED, Status.CONFIRMED, Status.REFUTED},
    Status.INFERRED: {Status.INFERRED, Status.CONFIRMED, Status.REFUTED},
    Status.CONFIRMED: {Status.CONFIRMED, Status.REFUTED},
    Status.REFUTED: set(),
}
_NEEDS_AUTHORITY = {Status.CONFIRMED, Status.REFUTED}


class Ledger:
    def __init__(self, path: str | Path | None = None, *,
                 trace_validator: Callable[[str], bool] | None = None):
        """`path` is a JSONL event log; None keeps everything in memory.

        `trace_validator`, if given, must return True for every trace ID cited.
        """
        self._lock = threading.RLock()
        self._path = Path(path) if path else None
        self._trace_ok = trace_validator
        self._claims: dict[str, Claim] = {}
        self._keys: dict[str, str] = {}
        self._findings: dict[str, Finding] = {}
        self._predictions: dict[str, Prediction] = {}
        self._outcomes: dict[str, Outcome] = {}
        self._scorer_token: ScorerAuthority | None = None
        self._scorer_granted = False
        if self._path and self._path.exists():
            for line in self._path.read_text().splitlines():
                if line.strip():
                    self._apply(json.loads(line))
        elif self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- event log ----------
    def _commit(self, event: dict[str, Any]) -> None:
        if self._path:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        self._apply(event)

    def _apply(self, event: dict[str, Any]) -> None:
        op = event["op"]
        if op == "claim_added":
            claim = Claim.model_validate(event["claim"])
            self._claims[claim.id] = claim
            if claim.key:
                self._keys[claim.key] = claim.id
        elif op == "status":
            claim = self._claims[event["claim_id"]]
            new = Status(event["status"])
            claim.status = new
            for trace_id in event["evidence"]:
                if trace_id not in claim.evidence:
                    claim.evidence.append(trace_id)
            if new is Status.CONFIRMED:
                claim.tier = Tier.OBSERVED  # traces now back it
            if event.get("reason"):
                claim.notes.append(f"{event['status']}: {event['reason']}")
            claim.updated_at = event["at"]
        elif op == "demoted":
            claim = self._claims[event["claim_id"]]
            claim.status = Status.INFERRED
            claim.notes.append(f"demoted: {event['reason']}")
            claim.updated_at = event["at"]
        elif op == "experiment":
            claim = self._claims[event["claim_id"]]
            if event["experiment_id"] not in claim.experiment_ids:
                claim.experiment_ids.append(event["experiment_id"])
            claim.updated_at = event["at"]
        elif op == "retested":
            claim = self._claims[event["claim_id"]]
            for trace_id in event["trace_ids"]:
                if trace_id not in claim.retested_in:
                    claim.retested_in.append(trace_id)
            claim.updated_at = event["at"]
        elif op == "note":
            claim = self._claims[event["claim_id"]]
            claim.notes.append(event["note"])
            claim.updated_at = event["at"]
        elif op == "finding_added":
            finding = Finding.model_validate(event["finding"])
            self._findings[finding.id] = finding
        elif op == "prediction_logged":
            prediction = Prediction.model_validate(event["prediction"])
            self._predictions[prediction.id] = prediction
        elif op == "outcome_recorded":
            outcome = Outcome.model_validate(event["outcome"])
            self._outcomes[outcome.prediction_id] = outcome
        else:
            raise LedgerError(f"unknown event {op!r}")

    # ---------- authority ----------
    def grant_scorer(self) -> ScorerAuthority:
        """Hand the scorer its token. Works once per Ledger instance."""
        with self._lock:
            if self._scorer_granted:
                raise LedgerError("scorer authority was already granted")
            self._scorer_granted = True
            self._scorer_token = ScorerAuthority()
            return self._scorer_token

    # ---------- validation helpers ----------
    def _check_traces(self, trace_ids: Iterable[str]) -> list[str]:
        ids = list(trace_ids)
        if any(not isinstance(t, str) or not t.strip() for t in ids):
            raise LedgerError("trace IDs must be non-empty strings")
        if self._trace_ok:
            unknown = [t for t in ids if not self._trace_ok(t)]
            if unknown:
                raise LedgerError(f"unknown trace IDs: {unknown[:5]}")
        return ids

    def _claim(self, claim_id: str) -> Claim:
        try:
            return self._claims[claim_id]
        except KeyError:
            raise LedgerError(f"no such claim {claim_id}") from None

    # ---------- claims ----------
    def add_claim(self, *, text: str, source: Source | str, created_by: str,
                  key: str | None = None, kind: str = "behavior",
                  subkind: str | None = None, topic: str | None = None,
                  rule: dict[str, Any] | None = None,
                  notes: Iterable[str] = ()) -> Claim:
        """Create an OPEN claim. A repeated `key` returns the existing claim."""
        with self._lock:
            if key and key in self._keys:
                return self.get(self._keys[key])
            if not text.strip():
                raise LedgerError("a claim needs text")
            if rule is not None and "type" not in rule:
                raise LedgerError("a rule needs a 'type'")
            source = Source(source)
            claim = Claim(
                id=f"c-{len(self._claims) + 1:04d}", key=key, text=text.strip(),
                kind=kind, subkind=subkind, topic=topic, rule=rule, source=source,
                tier=TIER_FOR_SOURCE[source], created_by=created_by, notes=list(notes),
            )
            self._commit({"op": "claim_added", "claim": claim.model_dump(mode="json")})
            return self.get(claim.id)

    def set_status(self, claim_id: str, status: Status | str, evidence: Iterable[str],
                   *, authority: ScorerAuthority | None = None, reason: str = "") -> Claim:
        with self._lock:
            claim = self._claim(claim_id)
            status = Status(status)
            if status is Status.OPEN:
                raise LedgerError("a claim cannot be set back to open")
            if status not in _ALLOWED[claim.status]:
                raise LedgerError(f"cannot move {claim_id} from {claim.status.value} "
                                  f"to {status.value}")
            ids = self._check_traces(evidence)
            if not ids:
                raise LedgerError(f"{status.value} needs at least one trace ID")
            if status in _NEEDS_AUTHORITY and (
                    authority is None or authority is not self._scorer_token):
                raise LedgerError(f"only the scorer may set {status.value}")
            self._commit({"op": "status", "claim_id": claim_id, "status": status.value,
                          "evidence": ids, "reason": reason, "at": now(),
                          "by": "scorer" if status in _NEEDS_AUTHORITY else "agent"})
            return self.get(claim_id)

    def demote(self, claim_id: str, reason: str) -> Claim:
        """Confirmed -> Inferred, for a rule that was not retested in new situations."""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.status is not Status.CONFIRMED:
                raise LedgerError("only confirmed claims can be demoted")
            self._commit({"op": "demoted", "claim_id": claim_id, "reason": reason, "at": now()})
            return self.get(claim_id)

    def mark_retested(self, claim_id: str, trace_ids: Iterable[str]) -> Claim:
        with self._lock:
            self._claim(claim_id)
            ids = self._check_traces(trace_ids)
            if not ids:
                raise LedgerError("retest needs at least one trace ID")
            self._commit({"op": "retested", "claim_id": claim_id, "trace_ids": ids, "at": now()})
            return self.get(claim_id)

    def demote_unretested(self, reason: str = "not retested in a new situation") -> list[str]:
        """Demote every confirmed claim that has no retest. Returns demoted IDs."""
        with self._lock:
            targets = [c.id for c in self._claims.values()
                       if c.status is Status.CONFIRMED and not c.retested_in]
            for claim_id in targets:
                self.demote(claim_id, reason)
            return targets

    def link_experiment(self, claim_id: str, experiment_id: str) -> Claim:
        with self._lock:
            self._claim(claim_id)
            self._commit({"op": "experiment", "claim_id": claim_id,
                          "experiment_id": experiment_id, "at": now()})
            return self.get(claim_id)

    def add_note(self, claim_id: str, note: str) -> Claim:
        with self._lock:
            self._claim(claim_id)
            self._commit({"op": "note", "claim_id": claim_id, "note": note, "at": now()})
            return self.get(claim_id)

    # ---------- findings ----------
    def add_finding(self, *, claim_id: str, artifact: str, artifact_said: str,
                    traces_said: str, trace_ids: Iterable[str],
                    kind: FindingKind | str) -> Finding:
        with self._lock:
            self._claim(claim_id)
            kind = FindingKind(kind)
            ids = self._check_traces(trace_ids)
            if kind is not FindingKind.MISSING and not ids:
                raise LedgerError("a finding needs trace IDs (except 'missing')")
            finding = Finding(id=f"f-{len(self._findings) + 1:04d}", claim_id=claim_id,
                              artifact=artifact, artifact_said=artifact_said,
                              traces_said=traces_said, trace_ids=ids, kind=kind)
            self._commit({"op": "finding_added", "finding": finding.model_dump(mode="json")})
            return finding

    # ---------- predictions ----------
    def log_prediction(self, *, game_id: str, step: int, prediction: dict[str, Any],
                       claim_ids: Iterable[str] = ()) -> Prediction:
        """Write the prediction BEFORE the action runs. It can never be edited."""
        with self._lock:
            ids = list(claim_ids)
            for claim_id in ids:
                self._claim(claim_id)
            seq = len(self._predictions) + 1
            record = Prediction(id=f"p-{seq:06d}", seq=seq, game_id=game_id, step=step,
                                prediction=prediction, claim_ids=ids)
            self._commit({"op": "prediction_logged", "prediction": record.model_dump(mode="json")})
            return record

    def record_outcome(self, prediction_id: str, actual: dict[str, Any], *,
                       surprise: bool, trace_id: str | None = None) -> Outcome:
        with self._lock:
            if prediction_id not in self._predictions:
                raise LedgerError(f"no such prediction {prediction_id}")
            if prediction_id in self._outcomes:
                raise LedgerError("outcome already recorded for this prediction")
            if trace_id is not None:
                self._check_traces([trace_id])
            outcome = Outcome(prediction_id=prediction_id, actual=actual,
                              surprise=surprise, trace_id=trace_id)
            self._commit({"op": "outcome_recorded", "outcome": outcome.model_dump(mode="json")})
            return outcome

    # ---------- queries ----------
    def get(self, claim_id: str) -> Claim:
        return self._claim(claim_id).model_copy(deep=True)

    def get_by_key(self, key: str) -> Claim | None:
        claim_id = self._keys.get(key)
        return self.get(claim_id).model_copy(deep=True) if claim_id else None

    def all_claims(self) -> list[Claim]:
        return [c.model_copy(deep=True) for c in self._claims.values()]

    def by_status(self, status: Status | str) -> list[Claim]:
        return [c for c in self.all_claims() if c.status is Status(status)]

    def by_tier(self, tier: Tier | str) -> list[Claim]:
        return [c for c in self.all_claims() if c.tier is Tier(tier)]

    def by_source(self, source: Source | str) -> list[Claim]:
        return [c for c in self.all_claims() if c.source is Source(source)]

    def by_topic(self, topic: str) -> list[Claim]:
        return [c for c in self.all_claims() if c.topic == topic]

    def open_questions(self) -> list[Claim]:
        return self.by_status(Status.OPEN)

    def findings(self, claim_id: str | None = None) -> list[Finding]:
        return [f.model_copy(deep=True) for f in self._findings.values()
                if claim_id is None or f.claim_id == claim_id]

    def predictions(self) -> list[Prediction]:
        return [p.model_copy(deep=True) for p in self._predictions.values()]

    def outcome(self, prediction_id: str) -> Outcome | None:
        found = self._outcomes.get(prediction_id)
        return found.model_copy(deep=True) if found else None

    def surprises(self) -> list[tuple[Prediction, Outcome]]:
        return [(self._predictions[pid].model_copy(deep=True), out.model_copy(deep=True))
                for pid, out in self._outcomes.items() if out.surprise]

    def counts(self) -> dict[str, int]:
        result = {s.value: 0 for s in Status}
        for claim in self._claims.values():
            result[claim.status.value] += 1
        return result
