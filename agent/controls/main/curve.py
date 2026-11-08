"""T12.3: the learning curve over probe count, and the zero-probe baseline."""
from __future__ import annotations

from typing import Any

from auditor.main.corpus import GameRecord

from .replay import RuleSpec, rescore


def learning_curve(rules: list[RuleSpec], seed_games: list[GameRecord], probe_games: list[GameRecord],
                   points: list[int] | None = None) -> list[dict[str, Any]]:
    """Re-score with the seed corpus plus the first k probe games, for each k.

    k = 0 is the zero-probe baseline: rules that fit the recorded games but were never
    tested by a fresh experiment, so none can be Confirmed.
    """
    total = len(probe_games)
    ks = sorted({0, *(k for k in (points or default_points(total)) if 0 <= k <= total)})
    rows = []
    for k in ks:
        rows.append({"k": k, **rescore(rules, seed_games + probe_games[:k]).summary()})
    return rows


def default_points(total: int) -> list[int]:
    steps = [0, 1, 2, 3, 5, 8, 12, 20, 30, total]
    return [k for k in steps if k <= total]


def baseline_is_low(curve: list[dict[str, Any]]) -> bool:
    """The check T12.3 asks for: with no probes nothing is Confirmed, and the end does better."""
    first, last = curve[0], curve[-1]
    return first["k"] == 0 and first["confirmed"] == 0 and last["confirmed"] > first["confirmed"]
