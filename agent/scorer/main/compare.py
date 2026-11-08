"""How a rule's prediction is compared with what the binary actually did."""
from __future__ import annotations

import json
from typing import Any


def _norm_actions(actions: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(a, sort_keys=True) for a in actions)


def matches(kind: str, covers: list[str], prediction: Any, step: dict[str, Any]) -> bool:
    """True if `prediction` (not None) agrees with the recorded step."""
    if kind == "events":
        actual = [e for e in step["events"] if e["type"] in covers]
        return prediction == actual
    if kind == "legal":
        return isinstance(prediction, list) and _norm_actions(prediction) == _norm_actions(step["legal"])
    if kind == "view":
        return prediction == step["view"]
    raise ValueError(f"step comparison is not defined for kind {kind!r}")


def matches_setup(prediction: Any, game_initial_view: dict[str, Any]) -> bool:
    return prediction == game_initial_view


def summarize_actual(kind: str, covers: list[str], step: dict[str, Any]) -> Any:
    """What the rule should have predicted, for logging and for feedback prompts."""
    if kind == "events":
        return [e for e in step["events"] if e["type"] in covers]
    if kind == "legal":
        return step["legal"]
    return step["view"]
