"""T15.4: the "Next:" line that follows each action.

It is a pure function of the events written so far (and the fixed list of seed
experiments), so it only promises what the loop state supports and replays identically.
"""
from __future__ import annotations

from .events import Event
from .lines import clean


def _pending_titles(history: list[Event]) -> list[str]:
    """Titles of seed experiments that no probe event has run yet, in plan order."""
    from prober.main.experiments import seed_experiments       # lazy: keeps the feed light
    done = {e.data.get("experiment") for e in history if e.type == "probe"}
    return [x.title for x in seed_experiments() if x.id not in done]


def next_line(history: list[Event]) -> str | None:
    """`Next: ...` for the last event in `history`, or None when nothing is promised."""
    if not history:
        return None
    event = history[-1]
    data = event.data
    if data.get("redacted"):
        return None
    if event.type == "survey":
        return "Next: check those claims against the recorded games."
    if event.type in ("audit", "probe"):
        pending = _pending_titles(history)
        if pending:
            return f"Next: test '{clean(pending[0], 60)}'."
        return "Next: ask for rules that explain what was seen."
    if event.type == "hypothesize":
        return "Next: score the new rules against the recorded games."
    if event.type == "score":
        return "Next: keep scoring, then rebuild from the proven rules."
    if event.type == "rebuild":
        if data.get("runnable") is False:
            return "Next: ask for a fix, since the rebuild does not run."
        return "Next: check the rebuild against the original game."
    if event.type == "verify":
        if str(data.get("verdict", "")).lower() == "accepted":
            return "Next: write up the results."
        return "Next: fix the rebuild from the failing game, if attempts remain."
    if event.type == "repair":
        return "Next: rebuild with the fix."
    return None
