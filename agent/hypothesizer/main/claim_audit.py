"""T6.6: SubQ reads each claim's cited traces and says supported / unsupported / ambiguous.

It is a second pair of eyes from a different model family. It never changes a status:
disagreements are written to a list a human can read, and noted on the claim.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from auditor.main.corpus import TraceStore
from auditor.main.digest import step_line
from ledger.main.models import Claim, Status
from ledger.main.store import Ledger
from llm.main.subq import SubqError

OPTIONS = {
    "supported": "The cited traces directly show what the claim says",
    "unsupported": "The cited traces contradict the claim or do not show it",
    "ambiguous": "The traces are consistent with the claim but also with other explanations",
}
# What we expect SubQ to say for a claim in each status.
EXPECTED = {
    Status.OBSERVED: {"supported"},
    Status.INFERRED: {"supported", "ambiguous"},
    Status.CONFIRMED: {"supported"},
    Status.REFUTED: {"unsupported"},
}


@dataclass
class AuditRow:
    claim_id: str
    status: str
    label: str
    confidence: float
    agrees: bool
    text: str


def render_evidence(store: TraceStore, claim: Claim, limit: int = 8) -> list[str]:
    lines = []
    for trace_id in claim.evidence[:limit]:
        found = store.find_step(trace_id)
        if found is None:
            continue
        game, step = found
        lines.append(step_line(step) if step.get("before") else f"{trace_id}: (no detail)")
    return lines


def claim_text(claim: Claim) -> str:
    rule = claim.rule or {}
    if rule.get("type") == "executable":
        return f"{claim.text}\nRule code:\n{rule['code']}"
    return claim.text


def audit_claims(subq: Any, ledger: Ledger, store: TraceStore, *, limit: int | None = None
                 ) -> list[AuditRow]:
    rows: list[AuditRow] = []
    claims = [c for c in ledger.all_claims() if c.status in EXPECTED and c.evidence]
    for claim in claims[:limit]:
        evidence = render_evidence(store, claim)
        if not evidence:
            continue
        question = ("Does the claim hold according to these recorded steps?"
                    if claim.status is not Status.REFUTED else
                    "Do these recorded steps contradict the claim?")
        state = {"claim": claim_text(claim), "claim_status_in_ledger": claim.status.value,
                 "cited_traces": evidence}
        try:
            answer = subq.choice(json.loads(json.dumps(state)), question, OPTIONS)
        except SubqError:
            continue
        label = answer.choice
        agrees = label in EXPECTED[claim.status]
        rows.append(AuditRow(claim.id, claim.status.value, label, answer.confidence, agrees,
                             claim.text[:120]))
        if not agrees:
            ledger.add_note(claim.id, f"SubQ audit disagrees: labelled '{label}' "
                                      f"(confidence {answer.confidence:.2f}) for a {claim.status.value} claim")
    return rows


def disagreements(rows: list[AuditRow]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows if not r.agrees]
