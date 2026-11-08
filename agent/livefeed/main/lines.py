"""T15.2: one fixed sentence template per event type. No model, so a line cannot say
something that did not happen.

    "[loop 12] Marked 'vent lowers pressure by 3' as wrong (trace t-000412)."
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import safety
from .events import EVENT_TYPES, Event

MAX_QUOTE = 70
MAX_LINE = 220
_SENTENCE_BREAK = re.compile(r"[.!?]+\s+")
_TAIL_PUNCT = " .;:,!?\u2026"


def clean(text: Any, limit: int = MAX_QUOTE) -> str:
    """Make free text safe to quote inside one sentence: one line, no inner full stops."""
    flat = " ".join(str(text).split())
    flat = _SENTENCE_BREAK.sub("; ", flat).strip(_TAIL_PUNCT)
    flat = flat.replace("'", "\u2019").replace("\u201c", "").replace("\u201d", "")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip(_TAIL_PUNCT) + "\u2026"


def _num(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _cite(event: Event, limit: int = 1) -> str:
    ids = event.trace_ids[:limit]
    return f" (trace {', '.join(ids)})" if ids else ""


def _survey(e: Event) -> str:
    d = e.data
    return (f"Read the manual and the partial code: found {_plural(_num(d.get('claims')), 'claim')} to check "
            f"and {_plural(_num(d.get('unknowns')), 'unknown')} to probe")


def _audit(e: Event) -> str:
    d = e.data
    return (f"Checked the claims against the recorded games: {_num(d.get('refuted'))} wrong, "
            f"{_num(d.get('supported'))} supported, {_num(d.get('undecided'))} undecided")


def _probe(e: Event) -> str:
    d = e.data
    what = clean(d.get("title") or d.get("label") or "a test game", 50)
    steps, surprises = _num(d.get("steps")), _num(d.get("surprises"))
    tail = (f"{_plural(surprises, 'move')} did not match the guesses" if surprises
            else "everything matched the guesses" if d.get("predicted") else "no guesses to compare yet")
    return f"Played a game ({what}): {_plural(steps, 'move')}, {tail}"


def _hypothesize(e: Event) -> str:
    d = e.data
    return (f"Asked for rules about {clean(d.get('group') or 'the game', 30)}: "
            f"{_plural(_num(d.get('proposed')), 'new candidate rule')} on round {_num(d.get('round')) or 1}")


def _score(e: Event) -> str:
    d = e.data
    claim = f"'{clean(d.get('text') or 'a claim')}'"
    outcome = d.get("outcome")
    if outcome == "refuted":
        return f"Marked {claim} as wrong{_cite(e)}"
    if outcome == "confirmed":
        held = _num(d.get("heldout"))
        on = f" on {_plural(held, 'held-out step')}" if held else ""
        return f"Confirmed {claim}{on}{_cite(e)}"
    return f"Scored {claim}{_cite(e)}"


def _rebuild(e: Event) -> str:
    d = e.data
    language = clean(d.get("language") or "JavaScript", 20)
    if d.get("runnable") is False:
        return f"Rebuilt the engine in {language}, but it does not run yet"
    return f"Rebuilt the engine in {language}: {_plural(_num(d.get('unknown')), 'behavior')} still unknown"


def _verify(e: Event) -> str:
    d = e.data
    games, diffs = _num(d.get("games")), _num(d.get("divergences"))
    verdict = str(d.get("verdict", "")).lower()
    if verdict == "accepted":
        return f"Checked the rebuild on {_plural(games, 'game')}: 0 differences, accepted"
    if verdict == "blocked":
        why = f" ({clean(d['reason'], 60)})" if d.get("reason") else ""
        return f"Checked the rebuild on {_plural(games, 'game')}: blocked{why}"
    return f"Checked the rebuild on {_plural(games, 'game')}: {_plural(diffs, 'difference')}"


def _repair(e: Event) -> str:
    d = e.data
    return f"Asked for a fix to the rebuild after a failed check (attempt {_num(d.get('attempt')) or 1})"


TEMPLATES: dict[str, Callable[[Event], str]] = {
    "survey": _survey, "audit": _audit, "probe": _probe, "hypothesize": _hypothesize,
    "score": _score, "rebuild": _rebuild, "verify": _verify, "repair": _repair,
}
assert set(TEMPLATES) == set(EVENT_TYPES), "every event type needs a template"


def sentence(event: Event) -> str:
    """The plain sentence for an event, with no loop prefix and a final full stop."""
    if event.data.get("redacted") or not safety.is_safe(event.data):
        return "Skipped an update because it referred to restricted material"
    template = TEMPLATES.get(event.type)
    try:
        text = template(event) if template else f"Did something ({clean(event.type, 20)})"
    except Exception:                                 # a malformed event must not break the feed
        text = f"Recorded a {clean(event.type, 20)} step"
    return text.rstrip(_TAIL_PUNCT) + "."


def line(event: Event) -> str:
    """`[loop N] sentence.` Exactly one line, checked by the prompt firewall."""
    text = f"[loop {event.loop}] {sentence(event)}"
    if len(text) > MAX_LINE:
        text = text[: MAX_LINE - 1].rstrip(_TAIL_PUNCT + ")") + "\u2026"
    if not safety.is_safe(text):
        return f"[loop {event.loop}] Skipped an update because it referred to restricted material."
    return text
