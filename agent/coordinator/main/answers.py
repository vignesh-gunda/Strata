"""T10.4: every answer is built from ledger queries and files the pipeline wrote.

No model is called in this module, so an answer cannot say something the ledger does
not hold. Anything the ledger lacks is reported as "not known yet".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ledger.main.models import Claim, Status
from ledger.main.store import Ledger

PLAIN_STATUS = {
    Status.CONFIRMED: "proven by testing",
    Status.INFERRED: "likely, not yet proven",
    Status.OBSERVED: "seen in play, not yet generalised",
    Status.OPEN: "still unknown",
    Status.REFUTED: "shown to be wrong",
}
_ORDER = [Status.CONFIRMED, Status.REFUTED, Status.INFERRED, Status.OBSERVED, Status.OPEN]
_STOP = {"the", "a", "an", "of", "is", "are", "to", "and", "in", "it", "on", "for", "why", "how",
         "what", "does", "do", "explain", "rule", "claim", "about", "me", "tell", "that", "this",
         "with", "when", "show", "evidence", "proof", "prove", "proven"}
NO_RUN = "No run yet. Say \"start\" and I will survey, probe, test rules and rebuild."


def _trace_cite(ids: list[str], limit: int = 3) -> str:
    shown = ", ".join(ids[:limit])
    more = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return f"trace {shown}{more}" if ids else "no trace yet"


def ledger_summary(ledger: Ledger) -> str:
    counts = ledger.counts()
    lines = [f"{len(ledger.all_claims())} claims in total:"]
    for status in _ORDER:
        if counts.get(status.value):
            lines.append(f"  {counts[status.value]:3} {PLAIN_STATUS[status]}")
    findings = ledger.findings()
    if findings:
        lines.append(f"{len(findings)} places where the manual or code disagreed with the traces.")
    confirmed = ledger.by_status(Status.CONFIRMED)
    if confirmed:
        lines.append("Proven so far:")
        for claim in confirmed[:8]:
            lines.append(f"  - {claim.text} [{claim.id}, {_trace_cite(claim.evidence, 1)}]")
        if len(confirmed) > 8:
            lines.append(f"  ... and {len(confirmed) - 8} more")
    return "\n".join(lines)


def open_questions(ledger: Ledger, limit: int = 10) -> str:
    claims = ledger.open_questions()
    if not claims:
        return "Nothing is open right now."
    lines = [f"{len(claims)} things are still unknown. First {min(limit, len(claims))}:"]
    for claim in claims[:limit]:
        lines.append(f"  - {claim.text} [{claim.id}]")
    return "\n".join(lines)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1}


def find_claim(ledger: Ledger, text: str) -> Claim | None:
    """Claim ID if the message has one, else the claim sharing the most words with it."""
    match = re.search(r"\bc-\d{3,}\b", text, re.I)
    if match:
        try:
            return ledger.get(match.group(0).lower())
        except ValueError:
            return None
    wanted = _tokens(text)
    if not wanted:
        return None
    best, best_score = None, 0
    for claim in ledger.all_claims():
        score = len(wanted & _tokens(f"{claim.text} {claim.topic or ''}"))
        if score > best_score:
            best, best_score = claim, score
    return best if best_score >= min(2, len(wanted)) else None


def explain_claim(ledger: Ledger, text: str) -> str:
    claim = find_claim(ledger, text)
    if claim is None:
        return "Not known yet: I have no recorded rule that matches that. Ask for the \"open questions\" to see what exists."
    lines = [f"{claim.text}  [{claim.id}]",
             f"Status: {PLAIN_STATUS[claim.status]}. Came from the {claim.source.value}."]
    if claim.status is Status.OPEN:
        lines.append("Not known yet: no test has decided this.")
    else:
        lines.append(f"Evidence: {_trace_cite(claim.evidence)}.")
    for finding in ledger.findings(claim.id):
        lines.append(f"The {finding.artifact} said: {finding.artifact_said}. "
                     f"The traces show: {finding.traces_said} ({_trace_cite(finding.trace_ids, 2)}).")
    if claim.notes:
        lines.append(f"Latest note: {claim.notes[-1]}")
    return "\n".join(lines)


def _one_rebuild(report: dict, folder: Path) -> list[str]:
    language = report.get("language") or "JavaScript"
    verdict = "accepted" if report.get("accepted") else "not accepted yet"
    lines = [f"{language} rebuild: {verdict} after {report.get('attempts', '?')} attempt(s)."]
    unknown = report.get("unknown_behaviors")
    if unknown:
        lines.append(f"{len(unknown)} behaviors still unknown: {', '.join(unknown[:5])}")
    elif unknown is not None:
        lines.append("No unknown behaviors left in the rebuild.")
    program = folder / "program" if (folder / "program").is_dir() else folder
    files = sorted(p.name for p in program.iterdir() if p.is_file()) if program.exists() else []
    if files:
        lines.append(f"Files in {program}: {', '.join(files)}")
    return lines


def rebuild_summary(run_dir: Path) -> str:
    """One block per language that has been rebuilt: `<rebuild folder>/<language>/report.json`."""
    from rebuilder.main.location import rebuild_dir
    folder = rebuild_dir(run_dir)
    lines: list[str] = []
    for path in sorted(folder.glob("*/report.json")) if folder.exists() else []:
        try:
            lines += _one_rebuild(json.loads(path.read_text()), path.parent)
        except ValueError:
            lines.append(f"A rebuild report in {path.parent} could not be read.")
    if lines:
        return "\n".join(lines)
    legacy = run_dir / "rebuild_report.json"        # runs made before rebuilds were stored per language
    if legacy.exists():
        try:
            return "\n".join(_one_rebuild(json.loads(legacy.read_text()), folder))
        except ValueError:
            return "A rebuild report exists but could not be read."
    if folder.exists():
        return "A rebuild folder exists but has no report yet."
    return "No rebuild yet. It is made after rules are proven."


def report_summary(run_dir: Path, ledger: Ledger | None) -> str:
    """Stand-in until the full report (T11) exists: a summary straight from run files."""
    full = run_dir / "report.md"
    if full.exists():
        return f"The full report is at {full}"
    if ledger is None:
        return "No report yet and no ledger to summarise."
    parts = ["The full report is not written yet. What is known so far:", ledger_summary(ledger)]
    wrong = [f for f in ledger.findings() if f.trace_ids]
    if wrong:
        parts.append("Where the manual or code was wrong:")
        for finding in wrong[:5]:
            parts.append(f"  - {finding.artifact}: \"{finding.artifact_said}\" "
                         f"({finding.kind.value}, {_trace_cite(finding.trace_ids, 1)})")
    parts.append(rebuild_summary(run_dir))
    return "\n".join(parts)
