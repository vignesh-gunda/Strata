"""T14.5: pull the evidence first, from the ledger on disk.

Long-context reading (SubQ) is used only to *pick* which claims bear on the question.
It never supplies facts: everything an answer says comes from the claims, findings and
trace IDs returned here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ledger.main.models import Claim, Finding, Status
from ledger.main.store import Ledger
from livefeed.main import safety

POOL = 40            # candidates shown to SubQ at most (it accepts up to 128 questions)
KEEP = 6             # claims handed to the writer
RELEVANT = 0.5
_STOP = {"the", "a", "an", "of", "is", "are", "to", "and", "in", "it", "on", "for", "why", "how", "what",
         "does", "do", "explain", "tell", "me", "about", "that", "this", "with", "when", "show", "you",
         "your", "can", "game", "please", "did", "was", "were", "i", "my", "any", "all", "there"}
_RANK = {Status.CONFIRMED: 0, Status.REFUTED: 1, Status.INFERRED: 2, Status.OBSERVED: 3, Status.OPEN: 4}
_WRONG_FIRST = {Status.REFUTED: 0, Status.CONFIRMED: 1, Status.INFERRED: 2, Status.OBSERVED: 3, Status.OPEN: 4}
# Words that ask about the whole picture rather than one claim. Only these may pull in claims that
# share no words with the question; anything else with no match means "not known yet".
BROAD = {"rules", "rule", "everything", "overview", "summary", "summarise", "summarize", "learned",
         "found", "find", "discovered", "wrong", "manual", "stale", "mistakes", "mistake", "proven", "know"}
_ABOUT_WRONG = {"wrong", "manual", "stale", "mistakes", "mistake", "find", "found"}


@dataclass
class Evidence:
    claim: Claim
    findings: list[Finding] = field(default_factory=list)
    score: float = 0.0

    @property
    def trace_ids(self) -> list[str]:
        ids = list(self.claim.evidence)
        for finding in self.findings:
            ids += [t for t in finding.trace_ids if t not in ids]
        return ids


class Decider(Protocol):
    def decide(self, state: Any, questions: dict[str, dict]) -> dict[str, dict]: ...


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1}


def _usable(claim: Claim) -> bool:
    """Claims whose text could carry restricted or secret material are never quoted."""
    return safety.is_safe(claim.text)


def _candidates(ledger: Ledger, question: str) -> list[Evidence]:
    wanted = _tokens(question)
    scored = []
    for claim in ledger.all_claims():
        if not _usable(claim):
            continue
        overlap = len(wanted & _tokens(f"{claim.text} {claim.topic or ''}"))
        scored.append((overlap, claim))
    matching = sorted((x for x in scored if x[0] > 0), key=lambda x: (-x[0], _RANK[x[1].status], x[1].id))
    chosen = matching[:POOL]
    if len(chosen) < 3 and wanted & BROAD:      # "explain the rules", "what was wrong": no one claim to match
        about_wrong = bool(wanted & _ABOUT_WRONG)
        order = _WRONG_FIRST if about_wrong else _RANK
        rest = sorted((x for x in scored if x[0] == 0 and x[1].status is not Status.OPEN
                       and (not about_wrong or x[1].status is Status.REFUTED or ledger.findings(x[1].id))),
                      key=lambda x: (order[x[1].status], x[1].id))
        chosen += rest[: POOL - len(chosen)]
    return [Evidence(claim, ledger.findings(claim.id), float(overlap)) for overlap, claim in chosen]


def _ask_subq(subq: Decider, question: str, pool: list[Evidence]) -> list[Evidence] | None:
    from llm.main.subq import noul_question
    state = {"question": question,
             "claims": [{"n": i, "text": e.claim.text, "status": e.claim.status.value} for i, e in enumerate(pool)]}
    questions = {f"c{i}": noul_question(f"Does claim number {i} help answer the question? Answer true only if it "
                                        "bears directly on what the question asks.") for i in range(len(pool))}
    try:
        answers = subq.decide(state, questions)
    except Exception:
        return None
    ranked = []
    for i, e in enumerate(pool):
        try:
            p = float(answers[f"c{i}"]["noul"])
        except (KeyError, TypeError, ValueError):
            continue
        if p >= RELEVANT:
            ranked.append(Evidence(e.claim, e.findings, p))
    return ranked


def retrieve(ledger: Ledger, question: str, *, subq: Decider | None = None, keep: int = KEEP) -> list[Evidence]:
    """The claims (with findings and trace IDs) that bear on `question`, best first."""
    pool = _candidates(ledger, question)
    if not pool:
        return []
    picked = _ask_subq(subq, question, pool) if subq is not None else None
    if picked is None or not picked:
        picked = pool                                    # SubQ unavailable or found nothing: keep the lexical order
    order = _WRONG_FIRST if _tokens(question) & _ABOUT_WRONG else _RANK
    picked = sorted(picked, key=lambda e: (order[e.claim.status], -e.score, e.claim.id))
    return picked[:keep]
