"""What the UI app (`ui/server.py`) reads from a run directory.

The UI's contract for `/api/ledger` is
    {"connected": bool, "claims": [{"id", "claim", "status", "evidence": [trace ids], "experiment"}]}
and for `/api/feed` one JSON object per loop:
    {"loop", "type", "line", "sentence", "at", "trace_ids"}
Both are built here so the UI never parses the agent's files itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator.main.reliability import load_ledger

from .events import Event
from .lines import line, sentence
from .next_step import next_line


def ui_ledger(run_dir: Path) -> dict[str, Any]:
    """The run's ledger in the UI's shape. Falls back to the last good copy, never writes."""
    view = load_ledger(Path(run_dir), snapshot=False)
    if view.ledger is None:
        return {"connected": False, "claims": [], **({"error": view.note} if view.degraded else {})}
    claims = [{"id": c.id, "claim": c.text, "status": c.status.value, "evidence": list(c.evidence),
               "experiment": c.experiment_ids[0] if c.experiment_ids else None}
              for c in view.ledger.all_claims()]
    result: dict[str, Any] = {"connected": True, "claims": claims}
    if view.degraded:
        result["note"] = view.note
    return result


def ui_event(event: Event, history: list[Event] | None = None) -> dict[str, Any]:
    """`history` is every event up to and including this one; it gives the "Next:" line."""
    upcoming = next_line(history) if history else None
    return {"loop": event.loop, "type": event.type, "line": line(event), "sentence": sentence(event),
            "next": upcoming, "at": event.at, "trace_ids": event.trace_ids}
