"""T4: check the survey's claims against the seed corpus before playing any game.

    python -m auditor.main.audit --run-id r-01 [--use-subq]

Uses train traces only. Refutations go through the Scorer (the only holder of the
ledger's authority); every artifact-vs-traces disagreement becomes a Finding.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledger.main.models import Claim, FindingKind, Source, Status
from ledger.main.store import Ledger
from livefeed.main.events import EventLog, safe_emit
from sandbox.main.digsite_fs import DigSiteFS
from scorer.main.scorer import Scorer
from surveyor.main.probes import build_probe_list

from .checkers import (CHECKERS, MAX_IDS, measure_hand_limit, measure_pressure_limit,
                       no_winner_endings)
from .corpus import FittingCorpus, TraceStore, load_games, load_seed_corpus
from .digest import step_line

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent
CREATED_BY = "auditor"


@dataclass
class AuditReport:
    refuted: list[dict[str, Any]] = field(default_factory=list)
    supported: list[dict[str, Any]] = field(default_factory=list)
    undecided: list[dict[str, Any]] = field(default_factory=list)
    measured: list[dict[str, Any]] = field(default_factory=list)
    findings: int = 0
    triage: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"refuted": len(self.refuted), "supported": len(self.supported),
                "undecided": len(self.undecided), "measured": len(self.measured),
                "findings": self.findings, "triage": len(self.triage)}


def _replacement_text(rule: dict, replacement: dict) -> str | None:
    kind, value = rule["type"], replacement["value"]
    if kind == "pressure_delta":
        return (f"{rule['card'].capitalize()} changes the pressure gauge by {value:+d} "
                "(observed in traces).")
    if kind == "scan_depth":
        return f"Scan reveals {value} card(s) of the draw pile (observed in traces)."
    if kind == "hand_limit":
        return f"Draw is withheld once a hand holds {value} cards, so the hand limit is {value}."
    if kind == "pressure_limit":
        return f"A player is eliminated when pressure reaches {value}."
    return None


def _add_measured(ledger: Ledger, report: AuditReport, *, key: str, text: str, rule: dict,
                  ids: list[str], topic: str, status: Status = Status.INFERRED) -> str:
    claim = ledger.add_claim(text=text, source=Source.TRACE, created_by=CREATED_BY, key=key,
                             topic=topic, rule=rule, notes=["measured from the seed corpus"])
    if claim.status is Status.OPEN:
        ledger.set_status(claim.id, status, ids, reason="measured from train traces")
        report.measured.append({"claim_id": claim.id, "text": text, "evidence": ids[:MAX_IDS]})
    return claim.id


def _measure_parameters(ledger: Ledger, corpus: FittingCorpus, report: AuditReport) -> None:
    hand = measure_hand_limit(corpus)
    if hand:
        _add_measured(ledger, report, key="trace:measured:hand_limit",
                      text=f"Draw is offered with {hand['offered_at']} cards and withheld at "
                           f"{hand['value']}: the hand limit is {hand['value']}.",
                      rule={"type": "hand_limit", "value": hand["value"]}, ids=hand["ids"],
                      topic="action:draw")
    limit = measure_pressure_limit(corpus)
    if limit:
        _add_measured(ledger, report, key="trace:measured:pressure_limit",
                      text=f"A player is eliminated when pressure reaches {limit['value']}.",
                      rule={"type": "pressure_limit", "value": limit["value"]}, ids=limit["ids"],
                      topic="overflow")
    sizes = Counter(len(g.initial_view["hands"][0]) for g in corpus.games())
    if len(sizes) == 1:
        (size,) = sizes
        ids = [g.steps[0]["trace_id"] for g in corpus.games() if g.steps][:MAX_IDS]
        _add_measured(ledger, report, key="trace:measured:deal_size",
                      text=f"Every player starts with {size} cards.",
                      rule={"type": "deal_size", "value": size}, ids=ids, topic="setup:deal")
    revealed: dict[int, list[str]] = {}
    for _, step in corpus.steps():
        for event in step["events"]:
            if event["type"] == "revealed" and step["before"]["draw_pile_size"] >= 3:
                revealed.setdefault(len(event["cards"]), []).append(step["trace_id"])
    if len(revealed) == 1:
        (depth,) = revealed
        _add_measured(ledger, report, key="trace:measured:scan_depth:scan",
                      text=f"Scan reveals {depth} card(s) when the pile holds 3 or more.",
                      rule={"card": "scan", "type": "scan_depth", "value": depth}, ids=revealed[depth], topic="card:scan")

    # Unknown parameters the traces now answer: record the value, keep the claim.
    answers = {"hand_limit": (hand or {}).get("value"), "hand_size": next(iter(sizes), None)
               if len(sizes) == 1 else None,
               "scan_depth": next(iter(revealed), None) if len(revealed) == 1 else None}
    evidence = {"hand_limit": (hand or {}).get("ids", []),
                "hand_size": [g.steps[0]["trace_id"] for g in corpus.games() if g.steps][:MAX_IDS],
                "scan_depth": [i for ids in revealed.values() for i in ids][:MAX_IDS]}
    for claim in ledger.all_claims():
        if claim.subkind == "parameter" and claim.status is Status.OPEN:
            name = (claim.rule or {}).get("name")
            if answers.get(name) is not None and evidence.get(name):
                ledger.add_note(claim.id, f"measured value: {answers[name]} (from traces)")
                ledger.set_status(claim.id, Status.OBSERVED, evidence[name],
                                  reason=f"value measured: {answers[name]}")


def _add_findings(ledger: Ledger, corpus: FittingCorpus, report: AuditReport) -> None:
    for claim in ledger.all_claims():
        if claim.subkind == "missing_function" and not ledger.findings(claim.id):
            ledger.add_finding(claim_id=claim.id, artifact="code",
                               artifact_said="function body lost from the recovered source",
                               traces_said="behavior must be inferred from traces and experiments",
                               trace_ids=[], kind=FindingKind.MISSING)
            report.findings += 1
    ids = no_winner_endings(corpus)
    for claim in ledger.by_source(Source.MANUAL):
        if (claim.rule or {}).get("type") == "win_last_standing" and ids \
                and not any(f.kind is FindingKind.INCOMPLETE for f in ledger.findings(claim.id)):
            ledger.add_finding(
                claim_id=claim.id, artifact="manual", artifact_said=claim.text,
                traces_said=f"{len(ids)} game(s) end with several players alive and no winner "
                            "(a stalemate); the manual does not mention this ending",
                trace_ids=ids[:MAX_IDS], kind=FindingKind.INCOMPLETE)
            report.findings += 1


def _triage(subq: Any, ledger: Ledger, corpus: FittingCorpus, report: AuditReport,
            limit: int = 6) -> None:
    """Ask SubQ which claim each counterexample step bears on (a second opinion)."""
    from llm.main.subq import SubqError
    steps = {s["trace_id"]: s for _, s in corpus.steps()}
    behavior = [c for c in ledger.all_claims() if c.kind == "behavior" and c.topic]
    for item in report.refuted:
        for trace_id in item["counter"][:2]:
            if len(report.triage) >= limit:
                return
            step = steps.get(trace_id)
            if step is None:
                continue
            card = step["action"].get("card")
            options = {c.id: c.text for c in behavior
                       if card and (c.topic or "").startswith(f"card:{card}")}
            options["other"] = "None of the above"
            if len(options) < 3:
                continue
            try:
                answer = subq.choice({"step": step_line(step)},
                                     "Which claim does this recorded step bear on most directly?",
                                     options)
            except SubqError as error:
                report.triage.append({"trace_id": trace_id, "error": str(error)})
                continue
            report.triage.append({"trace_id": trace_id, "label": answer.choice,
                                  "confidence": answer.confidence,
                                  "refuted_claim": item["claim_id"]})
            if answer.choice != "other":
                ledger.add_note(answer.choice, f"SubQ triage: {trace_id} bears on this claim "
                                               f"(confidence {answer.confidence:.2f})")


def run_audit(ledger: Ledger, scorer: Scorer, corpus: FittingCorpus, *, subq: Any = None,
              events: EventLog | None = None) -> AuditReport:
    report = AuditReport()
    for claim in ledger.open_questions():
        rule = claim.rule or {}
        checker = CHECKERS.get(rule.get("type"))
        if claim.kind != "behavior" or checker is None or claim.source not in (Source.MANUAL, Source.CODE):
            continue
        verdict = checker(corpus, rule)
        row = {"claim_id": claim.id, "source": claim.source.value, "text": claim.text,
               "detail": verdict.detail}
        if verdict.outcome == "refuted":
            scorer.refute(claim.id, verdict.counter, verdict.detail)
            traces_said = verdict.detail
            if verdict.replacement:
                text = _replacement_text(rule, verdict.replacement)
                if text:
                    traces_said = text
                    _add_measured(ledger, report,
                                  key="trace:measured:" + rule["type"] + (f":{rule['card']}" if rule.get("card") else ""),
                                  text=text, rule={**rule, "value": verdict.replacement["value"]},
                                  ids=verdict.replacement["ids"], topic=claim.topic or "")
            ledger.add_finding(
                claim_id=claim.id, artifact=claim.source.value, artifact_said=claim.text,
                traces_said=traces_said, trace_ids=verdict.counter,
                kind=FindingKind.STALE if claim.source is Source.MANUAL else FindingKind.WRONG)
            report.findings += 1
            report.refuted.append({**row, "counter": verdict.counter})
        elif verdict.outcome == "supported":
            ledger.set_status(claim.id, Status.INFERRED, verdict.support, reason=verdict.detail)
            report.supported.append({**row, "support": verdict.support})
        else:
            if not any(n.startswith("corpus cannot decide") for n in claim.notes):
                ledger.add_note(claim.id, f"corpus cannot decide: {verdict.detail}")
            report.undecided.append(row)
    _measure_parameters(ledger, corpus, report)
    _add_findings(ledger, corpus, report)
    if subq is not None:
        _triage(subq, ledger, corpus, report)
    safe_emit(events, "audit", {"refuted": len(report.refuted), "supported": len(report.supported),
                               "undecided": len(report.undecided)})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check survey claims against the seed corpus.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--use-subq", action="store_true")
    args = parser.parse_args(argv)

    run_dir = Path(args.runs_dir) / args.run_id
    store = TraceStore(load_seed_corpus(DigSiteFS(args.dig_site)))
    for game in load_games(run_dir / "probe_games.jsonl"):
        store.add_game(game)
    ledger = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)
    events = EventLog(run_dir)
    scorer = Scorer(ledger, store, events=events)
    subq = None
    if args.use_subq:
        from llm.main.subq import SubqClient
        subq = SubqClient()
    report = run_audit(ledger, scorer, store.fitting(), subq=subq, events=events)
    (run_dir / "audit.json").write_text(json.dumps(report.summary() | {
        "refuted": report.refuted, "undecided": [u["claim_id"] for u in report.undecided],
        "triage": report.triage}, indent=2))
    (run_dir / "probe_list.json").write_text(json.dumps(build_probe_list(ledger), indent=2))
    print(f"Audit {args.run_id}: {report.summary()}")
    for row in report.refuted:
        print(f"  REFUTED [{row['source']}] {row['text']}  -> {row['counter'][:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
