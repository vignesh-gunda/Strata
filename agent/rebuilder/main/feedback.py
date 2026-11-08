"""T16.10: a Verifier difference is evidence, not just a bug to patch.

When the rebuild and the original disagree, three things happen besides the repair:
  1. the shortest failing game is replayed on the original and stored as a new trace
     (`record_counterexample` in cli.py does that);
  2. the Confirmed rules the rebuild relied on for the part that disagreed are demoted to
     Inferred, so the next hypothesize round has to prove them again against the new trace;
  3. the difference becomes an Open claim and goes to the top of the probe list.

Nothing here can confirm or refute anything. It only lowers trust and adds questions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ledger.main.models import Status
from ledger.main.store import Ledger

# What the Verifier's "what" labels (replay) and kinds (fuzz) point at.
_WHAT_TO_GROUP = {"initial view": "setup", "legal actions": "legal", "legal": "legal", "new_game": "setup",
                  "illegal": "legal", "crash": "legal"}


def _event_types(reply: Any) -> list[str]:
    events = reply.get("events") if isinstance(reply, dict) else None
    return [e.get("type") for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def groups_touched(counterexample: dict[str, Any]) -> list[str]:
    """Which rule groups (see hypothesizer groups.py) the disagreement is about."""
    label = counterexample.get("what") or counterexample.get("kind")
    if label in _WHAT_TO_GROUP:
        return [_WHAT_TO_GROUP[label]]
    reference = counterexample.get("reference") or counterexample.get("recorded")
    candidate = counterexample.get("candidate")
    ref_events, cand_events = _event_types(reference), _event_types(candidate)
    groups: list[str] = []
    for left, right in zip(ref_events + [None] * len(cand_events), cand_events + [None] * len(ref_events)):
        if left != right:
            groups.extend(g for g in (left, right) if g and g not in groups)
    if (not groups and isinstance(reference, dict) and isinstance(candidate, dict)
            and ref_events == cand_events and reference != candidate):
        groups.append("view")          # same events, different state afterwards
    return groups


def _rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for position, item in enumerate(items, start=1):
        item["rank"] = position
    return items


def apply_divergence_feedback(ledger: Ledger, rules: list[dict[str, Any]], counterexample: dict[str, Any],
                              *, trace_id: str | None, probe_list_path: Path | None) -> dict[str, Any]:
    """Demote what the rebuild relied on, open the question, and put it first on the probe list."""
    groups = groups_touched(counterexample)
    summary: dict[str, Any] = {"groups": groups, "demoted": [], "opened": [], "trace_id": trace_id}
    if not groups:
        return summary
    cited = f" (trace {trace_id})" if trace_id else ""
    for rule in rules:
        if rule.get("group") not in groups:
            continue
        claim = ledger.get(rule["claim_id"])
        if claim.status is Status.CONFIRMED:
            ledger.demote(claim.id, f"the rebuild diverged from the original in {rule['group']}{cited}; "
                                    "this rule must be proven again against the new trace")
            summary["demoted"].append(claim.id)
    digest = hashlib.sha1(json.dumps(counterexample.get("actions", []), sort_keys=True,
                                     default=str).encode()).hexdigest()[:8]
    for group in groups:
        key = f"divergence:{group}:{digest}"
        text = (f"The rebuilt game and the original disagree about '{group}' after "
                f"{len(counterexample.get('actions', []))} action(s){cited}.")
        claim = ledger.add_claim(text=text, source="binary", created_by="rebuilder-feedback", key=key,
                                 kind="unknown", subkind="rebuild_divergence", topic=f"divergence:{group}")
        summary["opened"].append(claim.id)
        if probe_list_path is not None:
            _prepend_probe(probe_list_path, claim.id, key, group, text,
                           [r["claim_id"] for r in rules if r.get("group") == group])
    return summary


def _prepend_probe(path: Path, claim_id: str, key: str, group: str, text: str, related: list[str]) -> None:
    items = json.loads(path.read_text()) if path.exists() else []
    if any(item.get("claim_key") == key for item in items):
        return                                  # already on the list
    items.insert(0, {
        "rank": 1, "claim_id": claim_id, "claim_key": key, "priority_class": 0, "class_name": "divergence",
        "kind": "unknown", "subkind": "rebuild_divergence", "topic": f"divergence:{group}", "source": "binary",
        "text": text, "reason": "A rebuilt engine built from proven rules disagreed with the original here.",
        "related_claim_ids": related,
        "experiment_hint": f"Replay the failing game and vary it around the '{group}' behaviour to see what the "
                           "rules missed."})
    path.write_text(json.dumps(_rank(items), indent=2))
