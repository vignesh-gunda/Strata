"""T11.1, T11.2 and T11.4: write the report from the ledger and the run's files.

Every statement the report makes about the game cites trace IDs that exist in the trace
store. If one cannot, `build_report` raises and nothing is written: a report with an
unbacked claim is a failed build, not a warning.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ledger.main.models import Claim, Finding, Status
from ledger.main.store import Ledger
from livefeed.main.events import read_events

from .reveal import MutationRow

PLAIN = {Status.CONFIRMED: "Confirmed", Status.INFERRED: "Inferred", Status.OBSERVED: "Observed",
         Status.OPEN: "Open", Status.REFUTED: "Refuted"}
NEEDS_TRACES = (Status.CONFIRMED, Status.INFERRED, Status.OBSERVED, Status.REFUTED)


class ReportError(RuntimeError):
    """The report cannot be built honestly. `problems` lists every offending claim."""

    def __init__(self, problems: list[str]):
        super().__init__("report refused: " + "; ".join(problems[:6]) + (" ..." if len(problems) > 6 else ""))
        self.problems = problems


@dataclass
class Report:
    markdown: str
    data: dict[str, Any]
    problems: list[str] = field(default_factory=list)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _ids(ids: list[str], limit: int = 4) -> str:
    shown = ", ".join(ids[:limit])
    return shown + (f" (+{len(ids) - limit})" if len(ids) > limit else "")


def validate_citations(ledger: Ledger, trace_exists: Callable[[str], bool],
                       mutation_rows: list[MutationRow] | None = None) -> list[str]:
    """T11.4: every non-open claim, every finding that has traces, every found mutation must cite real traces."""
    problems = []
    for claim in ledger.all_claims():
        if claim.status in NEEDS_TRACES:
            if not claim.evidence:
                problems.append(f"{claim.id} is {claim.status.value} but cites no trace")
            problems += [f"{claim.id} cites unknown trace {t}" for t in claim.evidence if not trace_exists(t)]
    for finding in ledger.findings():
        if finding.kind.value != "missing" and not finding.trace_ids:
            problems.append(f"{finding.id} cites no trace")
        problems += [f"{finding.id} cites unknown trace {t}" for t in finding.trace_ids if not trace_exists(t)]
    for row in mutation_rows or []:
        if row.found and not row.trace_ids:
            problems.append(f"mutation {row.name} is marked found but cites no trace")
        problems += [f"mutation {row.name} cites unknown trace {t}" for t in row.trace_ids if not trace_exists(t)]
    return problems


def _claims_table(claims: list[Claim], with_experiments: bool = False) -> list[str]:
    if not claims:
        return ["_None._", ""]
    head = "| Claim | Source | Evidence (trace IDs) |" + (" Experiments |" if with_experiments else "")
    rule = "|---|---|---|" + ("---|" if with_experiments else "")
    rows = [head, rule]
    for c in claims:
        row = f"| {_cell(c.text)} `{c.id}` | {c.source.value} | {_ids(c.evidence)} |"
        if with_experiments:
            row += f" {_ids(c.experiment_ids) or 'none'} |"
        rows.append(row)
    return rows + [""]


def _wrong_table(ledger: Ledger) -> tuple[list[str], list[dict[str, Any]]]:
    rows, data = [], []
    lost = []
    for f in ledger.findings():
        claim = ledger.get(f.claim_id)
        if f.kind.value == "missing":
            lost.append((claim, f))
            continue
        verdict = PLAIN[claim.status]
        data.append({"artifact": f.artifact, "claim": f.artifact_said, "verdict": verdict, "kind": f.kind.value,
                     "traces_said": f.traces_said, "trace_ids": f.trace_ids, "claim_id": claim.id})
        rows.append(f"| {_cell(f.artifact.capitalize())} | {_cell(f.artifact_said)} | {verdict} ({f.kind.value}) "
                    f"| {_cell(f.traces_said)} | {_ids(f.trace_ids)} |")
    out = ["| Artifact | It said | Verdict | The recorded games show | Trace IDs |", "|---|---|---|---|---|"]
    out += rows or ["| _none found_ | | | | |"]
    out.append("")
    if lost:
        out += ["Information the artifacts lost (no trace can show this; it was probed instead):", ""]
        out += [f"- {_cell(c.text)} `{c.id}`" for c, _ in lost[:12]]
        out += [f"- ... and {len(lost) - 12} more" if len(lost) > 12 else "", ""]
    return out, data


def _equivalence(run_dir: Path, ledger: Ledger) -> tuple[list[str], dict[str, Any]]:
    lines, data = [], {"rebuilds": [], "equivalent_rules": []}
    verifies = [e for e in read_events(run_dir) if e.type == "verify"]
    from rebuilder.main.location import rebuild_dir
    folder = rebuild_dir(run_dir)
    reports = sorted(folder.glob("*/report.json")) if folder.exists() else []
    for path in reports:
        try:
            r = json.loads(path.read_text())
        except ValueError:
            continue
        last = r.get("history", [{}])[-1] if r.get("history") else {}
        row = {"language": r.get("language", path.parent.name), "accepted": bool(r.get("accepted")),
               "attempts": r.get("attempts"), "final": last.get("verdict"), "reason": last.get("reason", ""),
               "unknown": r.get("unknown_behaviors") or []}
        data["rebuilds"].append(row)
    if not data["rebuilds"]:
        lines.append("No rebuild has been checked yet, so nothing can be said about equivalence.")
    for row in data["rebuilds"]:
        state = "**Accepted**: no difference from the original found" if row["accepted"] \
            else "**Not accepted**: the checker still finds a difference"
        lines.append(f"- {row['language']}: {state} after {row['attempts']} attempt(s). "
                     f"Last check: {row['final']}. {len(row['unknown'])} behavior(s) left as unknown"
                     + (f": {', '.join(map(str, row['unknown'][:5]))}." if row["unknown"] else "."))
    if verifies:
        last = verifies[-1].data
        data["last_check"] = {"verdict": last.get("verdict"), "games": last.get("games"),
                              "divergences": last.get("divergences")}
        lines.append(f"- Most recent check: {last.get('verdict')} on {last.get('games', 0):,} fuzzed games "
                     f"(loop {verifies[-1].loop}).")
    twins = [(c.id, n) for c in ledger.all_claims() for n in c.notes if "equivalent to" in n]
    for cid, note in twins[:10]:
        data["equivalent_rules"].append({"claim_id": cid, "note": note})
    if twins:
        lines += ["", "Rules that predict the same thing on every recorded step are treated as one explanation:"]
        lines += [f"- `{cid}`: {_cell(note)}" for cid, note in twins[:10]]
    return lines + [""], data


def _mutation_section(rows: list[MutationRow] | None, refused: str | None) -> tuple[list[str], list[dict[str, Any]]]:
    if rows is None:
        return [f"_Not revealed: {refused or 'the answer key was not consulted'}._", ""], []
    out = ["| Planted change | Found by the run? | Matching claims | Trace IDs |", "|---|---|---|---|"]
    out += [f"| {_cell(r.what)} | {'Yes' if r.found else 'No'} | {_ids(r.claim_ids) or 'none'} "
            f"| {_ids(r.trace_ids) or 'none'} |" for r in rows]
    found = sum(r.found for r in rows)
    out += ["", f"{found} of {len(rows)} planted change(s) found. A match is by the words in the claim, so "
                "check each row by hand before relying on it.", ""]
    return out, [r.__dict__ for r in rows]


def build_report(run_dir: Path, ledger: Ledger, trace_exists: Callable[[str], bool], *,
                 mutation_rows: list[MutationRow] | None = None, mutation_refused: str | None = None,
                 now: datetime | None = None) -> Report:
    problems = validate_citations(ledger, trace_exists, mutation_rows)
    if problems:
        raise ReportError(problems)
    run_dir = Path(run_dir)
    counts = ledger.counts()
    by = lambda s: [c for c in ledger.all_claims() if c.status is s]
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    wrong_lines, wrong_data = _wrong_table(ledger)
    equiv_lines, equiv_data = _equivalence(run_dir, ledger)
    mut_lines, mut_data = _mutation_section(mutation_rows, mutation_refused)
    md = [f"# Strata report: run {run_dir.name}", "",
          f"Generated {stamp}. {len(ledger.all_claims())} claims: "
          + ", ".join(f"{counts.get(s.value, 0)} {s.value}" for s in
                      (Status.CONFIRMED, Status.INFERRED, Status.OBSERVED, Status.REFUTED, Status.OPEN)) + ".",
          "Every statement below that says something happened in the game cites trace IDs from the recorded "
          "or probed games. Open items have no trace by definition.", "",
          "## Where the evidence was wrong", ""] + wrong_lines
    md += ["## Observed", "", "Seen directly in the games, not yet generalised.", ""] + _claims_table(by(Status.OBSERVED))
    md += ["## Confirmed", "", "No mismatch on any recorded step, held-out games included, and separated "
           "from alternatives by an experiment.", ""] + _claims_table(by(Status.CONFIRMED), with_experiments=True)
    md += ["## Inferred and Open", "", "### Inferred (fits the traces, not yet proven)", ""] + _claims_table(by(Status.INFERRED))
    open_claims = by(Status.OPEN)
    md += [f"### Open ({len(open_claims)}: no trace decides these yet)", ""]
    md += [f"- {_cell(c.text)} `{c.id}`" for c in open_claims[:25]]
    md += [f"- ... and {len(open_claims) - 25} more" if len(open_claims) > 25 else "", ""]
    md += ["## Equivalence", ""] + equiv_lines
    md += ["## Mutation check", ""] + mut_lines
    data = {"run_id": run_dir.name, "generated": stamp, "counts": counts, "wrong_evidence": wrong_data,
            "equivalence": equiv_data, "mutations": mut_data,
            "mutations_revealed": mutation_rows is not None,
            "confirmed": [{"id": c.id, "text": c.text, "evidence": c.evidence} for c in by(Status.CONFIRMED)],
            "inferred": [{"id": c.id, "text": c.text, "evidence": c.evidence} for c in by(Status.INFERRED)],
            "open": [{"id": c.id, "text": c.text} for c in open_claims]}
    return Report("\n".join(md).rstrip() + "\n", data)
