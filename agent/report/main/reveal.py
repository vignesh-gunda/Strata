"""T11.3: the mutation check. The one place the planted mutations are read.

The answer key is read only here, only after the run has finished, and only to fill the
last section of the report. Nothing read here is returned to a model, put in a prompt, or
shown in chat: `report.md` is handed over by path, never quoted by the concierge.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator.main.reliability import RunState
from ledger.main.models import Claim, Status
from ledger.main.store import Ledger

# What each planted mutation changes, in plain words, and the words a claim about it would use.
# (Descriptions only; they say nothing about which mutations a given build planted.)
MUTATIONS: dict[str, dict[str, Any]] = {
    "vent_four": {"what": "vent lowers pressure by 4", "all": ["vent"], "any": ["4", "four"]},
    "stacking_double": {"what": "each double adds one more action", "all": ["double"],
                        "any": ["stack", "extra", "more action", "additional", "again"]},
    "hot_restart": {"what": "after an overflow pressure resets to 4", "all": ["overflow"],
                    "any": ["reset", "4", "four", "restart"]},
    "jam_blocks_draw": {"what": "a jam also wastes a draw", "all": ["jam"], "any": ["draw"]},
    "reverse_skips": {"what": "reverse also skips the next seat", "all": ["reverse"], "any": ["skip"]},
    "short_scan": {"what": "scan reveals only 1 card", "all": ["scan"], "any": ["1 card", "one card", "top card",
                                                                               "reveals 1", "shows 1", "1 of"]},
}
_DECIDED = (Status.CONFIRMED, Status.INFERRED, Status.REFUTED, Status.OBSERVED)


class RevealRefused(RuntimeError):
    pass


@dataclass
class MutationRow:
    name: str
    what: str
    found: bool
    claim_ids: list[str]
    trace_ids: list[str]


WORK_STAGES = ("survey", "audit", "hypothesize", "rebuild")


def run_finished(run_dir: Path) -> bool:
    """Only a run whose work is done may be checked against the answer key.

    The report is itself the last stage of a run, so "done" means every stage before it has
    completed (or the run is marked finished, or a standalone verifier verdict exists).
    """
    state = RunState.load(run_dir, run_dir.name)
    return state.finished or all(s in state.completed for s in WORK_STAGES) or (run_dir / "verdict.json").exists()


def read_planted(sealed_dir: Path, sealed_run_id: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", sealed_run_id):
        raise RevealRefused("the dig-site's run id is not valid")
    path = Path(sealed_dir) / f"{sealed_run_id}.json"
    if not path.is_file():
        raise RevealRefused("no answer key found for this build")
    planted = json.loads(path.read_text())["mutations"]
    names = []
    for item in planted:
        name = item if isinstance(item, str) else (item.get("name") or item.get("mutation") or item.get("id"))
        if name:
            names.append(str(name))
    return names


def _matches(claim: Claim, spec: dict[str, Any]) -> bool:
    text = claim.text.lower()
    return all(w in text for w in spec["all"]) and any(w in text for w in spec["any"])


def check_mutations(run_dir: Path, ledger: Ledger, sealed_dir: Path, sealed_run_id: str) -> list[MutationRow]:
    """Compare the planted mutations with what the run found. Refuses before the run has finished."""
    run_dir = Path(run_dir)
    if not run_finished(run_dir):
        raise RevealRefused("the run has not finished, so the answer key stays sealed")
    rows = []
    for name in read_planted(sealed_dir, sealed_run_id):
        spec = MUTATIONS.get(name)
        if spec is None:
            rows.append(MutationRow(name, "(a mutation this report has no description for)", False, [], []))
            continue
        hits = [c for c in ledger.all_claims() if c.status in _DECIDED and c.evidence and _matches(c, spec)]
        # a claim that says "vent lowers by 3" and is refuted shows the manual was wrong, not that
        # the real value was found: only measured or confirmed/inferred claims count as a find
        found_by = [c for c in hits if c.status is not Status.REFUTED]
        rows.append(MutationRow(name, spec["what"], bool(found_by), [c.id for c in found_by],
                                sorted({t for c in found_by for t in c.evidence})[:4]))
    return rows
