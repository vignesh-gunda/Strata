"""T12.1: traces only vs traces plus artifacts, replayed from a saved run.

The manual and the partial code do one thing in this system: they tell the Prober which
behaviors to test first (the ranked probe list, which seeds the experiments). So the two
arms differ only in the order of probes:

  * with artifacts   the seed experiments aimed at the lost functions and unknown parameters
                     first, then everything else in the order it was played
  * traces only      no probe list, so the generic policy games first, then the rest

Same rules, same scorer. We report how many probe games each arm needs before its Confirmed
set stops changing, and whether the two arms end with the same Confirmed rules. A
different end state is a finding, not something to hide.
"""
from __future__ import annotations

from typing import Any

from auditor.main.corpus import GameRecord

from .replay import RuleSpec, rescore


def is_seed(game: GameRecord, seed_ids: set[str]) -> bool:
    return game.meta.get("experiment_id") in seed_ids


def order_with_artifacts(probes: list[GameRecord], seed_ids: list[str]) -> list[GameRecord]:
    rank = {e: i for i, e in enumerate(seed_ids)}
    seeds = sorted((g for g in probes if g.meta.get("experiment_id") in rank), key=lambda g: rank[g.meta["experiment_id"]])
    return seeds + [g for g in probes if g.meta.get("experiment_id") not in rank]


def order_traces_only(probes: list[GameRecord], seed_ids: list[str]) -> list[GameRecord]:
    """Without the artifacts there is no seed list: policy games first, then designed experiments."""
    ids = set(seed_ids)
    rest = [g for g in probes if g.meta.get("experiment_id") not in ids]
    policy = [g for g in rest if not g.meta.get("experiment_id")]
    designed = [g for g in rest if g.meta.get("experiment_id")]
    return policy + designed


def step_signature(step: dict[str, Any]) -> tuple:
    """What kind of situation this step was: the action, the pressure and hand size it was taken at,
    how many players, and which kinds of events it caused."""
    action, before = step["action"], step.get("before") or {}
    seat = step.get("seat", 0)
    hands = before.get("hands") or []
    hand = len(hands[seat]) if seat < len(hands) and isinstance(hands[seat], list) else None
    return (action["type"], action.get("card"), before.get("pressure"), hand, before.get("players"),
            tuple(sorted({e["type"] for e in step.get("events", [])})))


def coverage_curve(seed_games: list[GameRecord], ordered: list[GameRecord], universe: set[tuple]) -> list[int]:
    """After each probe game, how many of the `universe` of step kinds have been seen at least once."""
    seen = {step_signature(s) for g in seed_games for s in g.steps}
    counts = []
    for game in ordered:
        seen |= {step_signature(s) for s in game.steps}
        counts.append(len(seen & universe))
    return counts


def probes_to_reach(counts: list[int], target: int) -> int | None:
    return next((i + 1 for i, c in enumerate(counts) if c >= target), None)


def run_arm(rules: list[RuleSpec], seed_games: list[GameRecord], ordered: list[GameRecord],
            points: list[int]) -> dict[str, Any]:
    ks = sorted({0, len(ordered), *(k for k in points if k <= len(ordered))})
    rows, confirmed = [], {}
    for k in ks:
        outcome = rescore(rules, seed_games + ordered[:k])
        rows.append({"k": k, **outcome.summary()})
        confirmed[k] = outcome.keys("confirmed")
    final = confirmed[ks[-1]]
    settled = ks[-1]
    for k in reversed(ks):                      # earliest k from which the Confirmed set never changes again
        if confirmed[k] != final:
            break
        settled = k
    return {"curve": rows, "final_confirmed": sorted(final), "probes_to_settle": settled}


def compare(rules: list[RuleSpec], seed_games: list[GameRecord], probes: list[GameRecord],
            seed_ids: list[str], points: list[int]) -> dict[str, Any]:
    arms = {"with_artifacts": order_with_artifacts(probes, seed_ids),
            "traces_only": order_traces_only(probes, seed_ids)}
    result: dict[str, Any] = {name: run_arm(rules, seed_games, ordered, points) for name, ordered in arms.items()}
    a, b = (set(result[n]["final_confirmed"]) for n in arms)
    result["same_confirmed_rules"] = a == b
    result["only_with_artifacts"] = sorted(a - b)
    result["only_traces_only"] = sorted(b - a)
    result["fewer_probes_with_artifacts"] = (result["with_artifacts"]["probes_to_settle"]
                                              < result["traces_only"]["probes_to_settle"])
    # Confirming a rule takes one fresh probe step either way, so the order of probes matters more for
    # how quickly rare behaviors get exercised. Step kinds seen is an artifact-free measure of that.
    universe = {step_signature(s) for g in probes for s in g.steps} - {step_signature(s) for g in seed_games for s in g.steps}
    for name, ordered in arms.items():
        counts = coverage_curve(seed_games, ordered, universe)
        result[name]["new_step_kinds_total"] = len(universe)
        result[name]["coverage"] = counts
        for pct in (50, 90, 100):
            result[name][f"probes_to_{pct}pct_coverage"] = probes_to_reach(counts, -(-len(universe) * pct // 100))
    result["fewer_probes_to_full_coverage_with_artifacts"] = _fewer(result, "probes_to_100pct_coverage")
    return result


def _fewer(result: dict[str, Any], key: str) -> bool:
    a, b = result["with_artifacts"][key], result["traces_only"][key]
    return a is not None and (b is None or a < b)
