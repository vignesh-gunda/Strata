"""Human-readable one-line rendering of traces for the live terminal feed."""

from __future__ import annotations

import os
import sys

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def paint(text: str, style: str) -> str:
    return f"\033[{_CODES[style]}m{text}\033[0m" if _COLOR else text


def _action(action: dict) -> str:
    return f"{action['type']} {action['card']}" if "card" in action else action["type"]


def _event(event: dict) -> str:
    kind = event["type"]
    if kind == "pressure":
        return paint(f"pressure={event['value']}", "yellow")
    if kind == "eliminated":
        return paint(f"seat {event['seat']} OUT", "red")
    if kind == "drew":
        return f"drew {event['card']}"
    if kind == "revealed":
        return paint(f"saw {event['cards']}", "cyan")
    if kind == "turn_passed":
        return paint(f"-> seat {event['to']} ({event['actions']} act)", "dim")
    if kind == "game_over":
        return paint(f"GAME OVER winner={event['winner']}", "green")
    if kind in ("played", "passed"):
        return ""
    return kind  # opaque event names (e1..eN) are shown as-is


def format_trace(trace: dict) -> str:
    """e.g. ``t-000123 g-0004 [train] T7 s1 play gear3 | pressure=9 -> seat 2 (1 act)``"""
    effects = [text for text in (_event(event) for event in trace["events"]) if text]
    changed = [event for event in trace["events"] if event["type"] not in ("played", "turn_passed")]
    no_effect = paint("(nothing else visible)", "red") if trace["action"]["type"] == "play" and not changed else ""
    head = f"{paint(trace['trace_id'], 'dim')} {trace['game_id']} [{trace['split']}] T{trace['turn']} s{trace['seat']}"
    return f"{head} {paint(_action(trace['action']), 'bold')} | {' '.join(effects)} {no_effect}".rstrip()


def format_game(game: dict, totals: dict) -> str:
    return paint(
        f"== {game['game_id']} {game['label']} players={game['players']} steps={game['steps']} "
        f"winner={game['winner']} | total games={totals['games']} traces={totals['traces']}",
        "green",
    )
