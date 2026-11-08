"""T6: propose competing rules with Muse, let the Scorer judge them, repair, separate.

    python -m hypothesizer.main.hypothesize --run-id r-01 [--groups legal,pressure] [--rounds 3]

Rules are code. Muse proposes; the Scorer (pure code) refutes, infers or confirms.
The Hypothesizer only ever sees the FittingCorpus (train data).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from auditor.main.corpus import TraceStore, load_games, load_seed_corpus
from ledger.main.models import Claim, FindingKind, Source, Status
from ledger.main.store import Ledger
from livefeed.main.events import EventLog, safe_emit
from llm.main.muse import MuseClient, MuseError
from prober.main.designer import design_experiments, select_experiment
from prober.main.probe import hand_size_for, known_cards, run_random_probes, run_seed_probes
from prober.main.runner import GameRunner, ProbeBudget
from sandbox.main.binary import BinaryProcess
from sandbox.main.digsite_fs import DigSiteFS
from sandbox.main.rule_worker import RuleError, RuleWorker
from hypothesizer.main.claim_audit import audit_claims, disagreements
from scorer.main.scorer import GroupReport, Scorer

from .groups import GROUP_ORDER, GROUPS
from .prompts import SYSTEM, build_prompt

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent


class RuleProposal(BaseModel):
    name: str
    description: str = ""
    code: str


class Proposals(BaseModel):
    rules: list[RuleProposal] = Field(default_factory=list)


@dataclass
class GroupResult:
    group: str
    rounds: int = 0
    added: list[str] = field(default_factory=list)
    report: GroupReport | None = None
    rejected: list[str] = field(default_factory=list)
    error: str | None = None

    def counts(self) -> dict[str, int]:
        r = self.report
        return {"confirmed": len(r.confirmed), "inferred": len(r.inferred),
                "refuted": len(r.refuted), "unscored": len(r.unscored)} if r else {}


class Hypothesizer:
    def __init__(self, muse: MuseClient, scorer: Scorer, store: TraceStore, ledger: Ledger, *,
                 lead_code: str = "", rules_per_round: int = 3, max_rounds: int = 3,
                 events: EventLog | None = None, subq: Any = None,
                 probe_list: list[dict[str, Any]] | None = None,
                 digest_games: int = 6, digest_steps: int = 45):
        self.events = events
        self.digest_games, self.digest_steps = digest_games, digest_steps   # how much of the traces Muse reads
        self.subq = subq                        # T16.9: picks which designed experiment to run next
        self.probe_list = probe_list or []
        self.muse, self.scorer, self.store, self.ledger = muse, scorer, store, ledger
        self.lead_code = lead_code
        self.rules_per_round = rules_per_round
        self.max_rounds = max_rounds

    # ---------- context ----------
    def _claims(self) -> list[Claim]:
        """Leads and measurements (manual, code, trace), never the model's own rules."""
        return [c for c in self.ledger.all_claims() if c.source is not Source.MODEL]

    def _findings_text(self) -> list[str]:
        out = []
        for f in self.ledger.findings():
            if f.kind in (FindingKind.STALE, FindingKind.WRONG, FindingKind.INCOMPLETE):
                out.append(f"{f.artifact} said \"{f.artifact_said}\" but traces say: "
                           f"{f.traces_said} (e.g. {', '.join(f.trace_ids[:2])})")
        return out

    def _accepted(self, group: str) -> list[Claim]:
        return [c for c in self.ledger.all_claims()
                if c.source is Source.MODEL and (c.rule or {}).get("type") == "executable"
                and c.rule.get("group") != group
                and c.status in (Status.INFERRED, Status.CONFIRMED)]

    def _feedback(self, group: str, claim_ids: list[str], report: GroupReport) -> list[dict[str, Any]]:
        """Counterexamples from fitting data only; held-out failures are reported without detail."""
        rows = []
        ranked = sorted(claim_ids, key=lambda cid: (report.scores[cid].mismatches
                                                    if cid in report.scores else 10**9))
        for claim_id in ranked[:2]:
            claim, score = self.ledger.get(claim_id), report.scores.get(claim_id)
            if score is None:
                continue
            if score.load_error:
                rows.append({"name": claim.text, "code": claim.rule["code"],
                             "problem": f"could not load: {score.load_error}"})
                continue
            fit_ids = [i for i in score.all_mismatch_ids() if self.store.split_of(i) != "heldout"]
            row: dict[str, Any] = {
                "name": claim.text, "code": claim.rule["code"],
                "wrong_on_fitting_steps": len(fit_ids),
                "applied_to_fitting_steps": score.applicable["train"] + score.applicable["probe"],
                "examples": self.scorer.explain(claim.rule["code"], fit_ids, limit=3)}
            if score.mismatch_ids["heldout"]:
                row["also_failed_held_out_check"] = True
            rows.append(row)
        return rows

    # ---------- proposing ----------
    def _propose(self, group: str, feedback: list[dict[str, Any]]) -> Proposals:
        prompt = build_prompt(
            group, corpus=self.store.fitting(), claims=self._claims(),
            findings_text=self._findings_text(), lead_code=self.lead_code,
            accepted=self._accepted(group), feedback=feedback, count=self.rules_per_round,
            games_in_digest=self.digest_games, digest_steps=self.digest_steps)
        return self.muse.complete_json(prompt, Proposals, system=SYSTEM)

    def _admit(self, group: str, proposals: Proposals, result: GroupResult) -> list[str]:
        spec = GROUPS[group]
        added = []
        for proposal in proposals.rules:
            digest = hashlib.sha1(proposal.code.encode()).hexdigest()[:12]
            worker = RuleWorker()
            try:
                meta = worker.load(proposal.code)
            except RuleError as error:
                # Keep it: the Scorer reports the load error back as feedback.
                meta = None
                result.rejected.append(f"{proposal.name}: {error}")
            finally:
                worker.close()
            if meta is not None and (meta["kind"] != spec.kind
                                     or (spec.covers and list(meta["covers"]) != list(spec.covers))):
                result.rejected.append(f"{proposal.name}: KIND/COVERS do not match group {group}")
                continue
            claim = self.ledger.add_claim(
                text=f"[{group}] {proposal.name}: {proposal.description}".strip(),
                source=Source.MODEL, created_by="muse", key=f"rule:{group}:{digest}",
                topic=f"rule:{group}",
                rule={"type": "executable", "group": group, "kind": spec.kind,
                      "covers": list(spec.covers), "code": proposal.code})
            added.append(claim.id)
        return added

    # ---------- one group ----------
    def run_group(self, group: str) -> GroupResult:
        result = GroupResult(group=group)
        feedback: list[dict[str, Any]] = []
        for round_no in range(1, self.max_rounds + 1):
            result.rounds = round_no
            try:
                proposals = self._propose(group, feedback)
            except MuseError as error:
                result.error = str(error)
                break
            new_ids = self._admit(group, proposals, result)
            result.added += new_ids
            safe_emit(self.events, "hypothesize",
                      {"group": group, "round": round_no, "proposed": len(new_ids)})
            report = self.scorer.adjudicate_group(group)
            result.report = report
            if report.inferred or report.confirmed:
                break                                     # at least one explanation survives
            feedback = self._feedback(group, new_ids or report.refuted + report.unscored, report)
        return result

    def run_groups(self, groups: list[str], workers: int = 4) -> list[GroupResult]:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.run_group, groups))

    # ---------- separating survivors by experiment ----------
    def separate(self, group: str, runner: GameRunner, hand_size: int, count: int = 2) -> GroupReport:
        report = self.scorer.adjudicate_group(group)
        if not report.needs_experiment:
            return report
        survivors = [self.ledger.get(cid) for cid in report.needs_experiment]
        hypotheses = [{"id": c.id, "text": c.text, "code": c.rule["code"]} for c in survivors]
        existing = {g.meta.get("experiment_id") for g in self.store.games()} - {None}
        sample = [s for g in self.store.fitting().games() for s in g.steps][:12]
        # With SubQ on, Muse designs a few extra candidates and SubQ picks the `count` most informative
        # ones, one at a time. Without SubQ (or if it fails) the first `count` run, as before.
        extra = 2 if self.subq is not None else 0
        try:
            designs, _rejected = design_experiments(
                self.muse, hand_size=hand_size, known_cards=known_cards(self.store),
                hypotheses=hypotheses, sample_steps=sample, existing_ids=existing, count=count + extra)
        except MuseError:
            return report
        if not designs:
            return report                      # nothing valid to run: no separation attempt was made
        for exp in self._choose(designs, count):
            runner.run_experiment(exp, hand_size)
        # An attempt to separate the survivors has now been made. Survivors that STILL predict
        # identically on every step are one hypothesis; confirm one representative.
        return self.scorer.adjudicate_group(group, collapse_equivalent=True)


    def _choose(self, designs: list, count: int) -> list:
        """The experiments to run, in order. SubQ only ranks; it never changes what is true."""
        if self.subq is None or len(designs) <= count:
            return designs[:count] if self.subq is None else designs
        remaining, chosen = list(designs), []
        while remaining and len(chosen) < count:
            pick = select_experiment(self.subq, self.ledger, remaining, self.probe_list)
            chosen.append(pick)
            remaining = [e for e in remaining if e.id != pick.id]
        return chosen


def confirmed_rules(ledger: Ledger) -> list[dict[str, Any]]:
    """The rules the Rebuilder may use: Confirmed only."""
    return [{"claim_id": c.id, "group": c.rule["group"], "kind": c.rule["kind"],
             "covers": c.rule["covers"], "text": c.text, "code": c.rule["code"],
             "evidence": c.evidence, "experiments": c.experiment_ids}
            for c in ledger.by_status(Status.CONFIRMED)
            if c.source is Source.MODEL and (c.rule or {}).get("type") == "executable"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propose, score and separate rules.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--groups", default=",".join(GROUP_ORDER))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--train-probes", type=int, default=6)
    parser.add_argument("--heldout-probes", type=int, default=8)
    parser.add_argument("--separate", type=int, default=1, help="experiment rounds per group")
    parser.add_argument("--no-subq", action="store_true",
                        help="do not use SubQ to pick experiments or audit claims (it is on when SUBQ_API_KEY is set)")
    parser.add_argument("--probe-steps", type=int, default=250, help="max moves per random probe game")
    parser.add_argument("--digest-games", type=int, default=6, help="whole games shown to Muse per prompt")
    parser.add_argument("--digest-steps", type=int, default=45, help="moves shown per game in the prompt")
    parser.add_argument("--quick", action="store_true",
                        help="short traces: 3+3 probe games of at most 40 moves, 2 rounds, small prompt digests")
    args = parser.parse_args(argv)
    if args.quick:
        # Enough to learn and separate rules, but far less to play and to read. Held-out probes stay
        # at 3 games so the "at least 3 held-out steps" bar for Confirmed can still be met.
        args.train_probes, args.heldout_probes = min(args.train_probes, 3), min(args.heldout_probes, 3)
        args.probe_steps, args.rounds = min(args.probe_steps, 40), min(args.rounds, 2)
        args.digest_games, args.digest_steps = min(args.digest_games, 3), min(args.digest_steps, 20)

    run_dir = Path(args.runs_dir) / args.run_id
    fs = DigSiteFS(args.dig_site)
    store = TraceStore(load_seed_corpus(fs))
    for game in load_games(run_dir / "probe_games.jsonl"):
        store.add_game(game)
    ledger = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)
    events = EventLog(run_dir)
    scorer = Scorer(ledger, store, events=events)
    muse = MuseClient()
    subq = None
    if not args.no_subq:                    # T16.9: SubQ takes part in a real run when its key is there
        try:
            from llm.main.subq import SubqClient
            subq = SubqClient()
        except RuntimeError as exc:
            print(f"(SubQ is off: {exc})")
    probe_path = run_dir / "probe_list.json"
    probe_list = json.loads(probe_path.read_text()) if probe_path.exists() else []
    lead_code = "\n\n".join(f"# {p}\n{fs.read_text(p)}" for p in ("source/engine.py", "source/state.py"))
    hypothesizer = Hypothesizer(muse, scorer, store, ledger, lead_code=lead_code, max_rounds=args.rounds,
                                events=events, subq=subq, probe_list=probe_list,
                                digest_games=args.digest_games, digest_steps=args.digest_steps)
    hand_size = hand_size_for(ledger, store)

    with BinaryProcess(fs) as box:
        runner = GameRunner(box, store, ledger=ledger, run_id=args.run_id, budget=ProbeBudget(),
                            save_path=run_dir / "probe_games.jsonl", events=events)
        run_seed_probes(runner, hand_size)                         # idempotent: skips done ones
        have = lambda split: sum(1 for g in store.games()
                                 if g.origin == "probe" and g.label.startswith("probe:") and g.split == split)
        run_random_probes(runner, max(0, args.train_probes - have("train")), "train",
                          first_seed=1000 + have("train"), max_steps=args.probe_steps)
        run_random_probes(runner, max(0, args.heldout_probes - have("heldout")), "heldout",
                          first_seed=5000 + have("heldout"), max_steps=args.probe_steps)
        groups = [g for g in args.groups.split(",") if g]
        results = hypothesizer.run_groups(groups, workers=args.workers)
        for _ in range(args.separate):
            for group in groups:
                hypothesizer.separate(group, runner, hand_size)

    summary = {r.group: {"rounds": r.rounds, **r.counts(), "error": r.error, "rejected": r.rejected[:3]}
               for r in results}
    summary["final"] = {g: scorer.adjudicate_group(g).__dict__ | {"scores": None} for g in groups}
    (run_dir / "hypotheses.json").write_text(json.dumps(summary, indent=2, default=str))
    if subq is not None:                    # T6.6: a second opinion on every claim; disagreements are for a human
        rows = audit_claims(subq, ledger, store, limit=60)
        (run_dir / "claim_audit.json").write_text(json.dumps(
            {"checked": len(rows), "disagreements": disagreements(rows)}, indent=2, default=str))
        print(f"SubQ claim audit: {len(rows)} checked, {len(disagreements(rows))} disagree "
              f"(see claim_audit.json) | subq tokens: {subq.budget.summary().get('subq')}")
    print("ledger:", ledger.counts(), "| muse tokens:", muse.budget.summary())
    for group in groups:
        s = summary["final"][group]
        print(f"  {group:12} confirmed={len(s['confirmed'])} inferred={len(s['inferred'])} "
              f"refuted={len(s['refuted'])} unscored={len(s['unscored'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
