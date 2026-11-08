"""Replay, fuzz, shrink and the Accepted / Blocked gate."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, Iterable

from auditor.main.corpus import GameRecord
from sandbox.main.binary import BinaryError, BinaryRequestError

from .driver import Divergence, FuzzChooser, ListChooser, SideBySide

DEFAULT_MIN_FUZZ = 5000


# ---------- replay: the recorded traces against the candidate ----------

@dataclass
class ReplayResult:
    games: int = 0
    steps: int = 0
    divergences: list[dict[str, Any]] = field(default_factory=list)


def replay_traces(cand: Any, games: Iterable[GameRecord], max_divergences: int = 5) -> ReplayResult:
    """Every recorded step must reproduce on the candidate: legal actions, events, view."""
    result = ReplayResult()
    for game in games:
        result.games += 1
        try:
            started = cand.new_game(game.players, game.script)
            gid = started["game_id"]
            problem = None
            if started["view"] != game.initial_view:
                problem = {"step": 0, "what": "initial view", "recorded": game.initial_view,
                           "candidate": started["view"]}
            for step in ([] if problem else game.steps):
                legal = cand.legal_actions(gid)
                if legal != step["legal"]:
                    problem = {"step": step["step"], "trace_id": step["trace_id"], "what": "legal actions",
                               "recorded": step["legal"], "candidate": legal}
                    break
                reply = cand.act(gid, step["action"])
                result.steps += 1
                if reply["events"] != step["events"] or reply["view"] != step["view"]:
                    problem = {"step": step["step"], "trace_id": step["trace_id"],
                               "what": "events" if reply["events"] != step["events"] else "view",
                               "recorded": {"events": step["events"], "view": step["view"]},
                               "candidate": reply}
                    break
        except (BinaryRequestError, BinaryError) as error:
            problem = {"step": -1, "what": f"candidate error: {error}", "game": game.game_id}
            try:
                cand.restart()
            except BinaryError:
                pass
        if problem:
            problem["game"] = game.game_id
            result.divergences.append(problem)
            if len(result.divergences) >= max_divergences:
                break
    return result


# ---------- fuzz ----------

@dataclass
class FuzzResult:
    games: int = 0
    steps: int = 0
    divergence: Divergence | None = None
    players: int = 0
    script: dict[str, Any] | None = None
    ended: dict[str, int] = field(default_factory=dict)      # how games ended, for coverage


def fuzz(pair: SideBySide, *, games: int, seed: int = 0, extra_scripts: list[tuple[int, dict]] | None = None,
         max_steps: int = 400, illegal_rate: float = 0.02) -> FuzzResult:
    """Random scripted games on both programs, compared step by step. Stops at the first divergence."""
    result = FuzzResult()
    rng = random.Random(seed)
    extras = list(extra_scripts or [])
    for n in range(games):
        if n < len(extras):
            players, script = extras[n]
        else:
            players = rng.choice([2, 3, 4])
            script = pair.ref.client.random_script(seed * 1_000_003 + n)
        outcome = pair.play(players, script, FuzzChooser(rng, n), max_steps=max_steps,
                            illegal_rate=illegal_rate, rng=rng)
        result.games += 1
        result.steps += max(outcome.steps, 0)
        if outcome.divergence:
            result.divergence, result.players, result.script = outcome.divergence, players, script
            return result
        last = "capped" if outcome.steps >= max_steps else "over"
        result.ended[last] = result.ended.get(last, 0) + 1
    return result


# ---------- shrink ----------

def _diverges(pair: SideBySide, players: int, script: dict, actions: list[dict]) -> Divergence | None:
    outcome = pair.play(players, script, ListChooser(actions), max_steps=len(actions) + 1)
    if outcome.steps == -1:
        return None                            # this subsequence is not playable
    return outcome.divergence


def shrink(pair: SideBySide, players: int, script: dict, actions: list[dict],
           divergence: Divergence, max_replays: int = 1500) -> tuple[list[dict], Divergence]:
    """Shrink the action list to a short game that still diverges.

    1. cut everything after the divergence, 2. delta-debug chunks, 3. repeatedly try
    removing every window of 1..8 consecutive actions (this finds cancelling pairs that
    chunk removal cannot). Stops after `max_replays` attempts.
    """
    budget = [max_replays]

    def attempt(sequence: list[dict]) -> Divergence | None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        return _diverges(pair, players, script, sequence)

    current = list(divergence.actions or actions)[:divergence.step]
    best = divergence
    if attempt(current) is None:
        return current, best
    size = 2
    while len(current) >= 2 and budget[0] > 0:
        chunk = max(1, len(current) // size)
        reduced = False
        for start in range(0, len(current), chunk):
            candidate = current[:start] + current[start + chunk:]
            found = attempt(candidate)
            if found is not None:
                current, best, reduced = candidate, found, True
                size = max(size - 1, 2)
                break
        if not reduced:
            if chunk == 1:
                break
            size = min(len(current), size * 2)

    changed = True
    while changed and budget[0] > 0:
        changed = False
        for width in range(min(8, len(current) - 1), 0, -1):
            start = 0
            while start + width <= len(current) and budget[0] > 0:
                candidate = current[:start] + current[start + width:]
                found = attempt(candidate) if candidate else None
                if found is not None:
                    current, best, changed = candidate, found, True
                else:
                    start += 1
    return current, _diverges(pair, players, script, current) or best


def _candidate_error(cand: Any, players: int, script: dict, actions: list[dict]) -> str | None:
    """The candidate's own error message on the counterexample, if it raised one."""
    try:
        gid = cand.new_game(players, script)["game_id"]
        for action in actions:
            cand.act(gid, action)
        cand.legal_actions(gid)
    except BinaryRequestError as error:
        return f"{error.kind}: {error.message}"[:500]
    except BinaryError as error:
        return f"{type(error).__name__}: {error}"[:500]
    return None


# ---------- the gate ----------

@dataclass
class Verdict:
    verdict: str                                   # Accepted | Blocked
    reason: str
    replay: ReplayResult
    fuzz: FuzzResult
    counterexample: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason,
                "replay": {"games": self.replay.games, "steps": self.replay.steps,
                           "divergences": self.replay.divergences[:3]},
                "fuzz": {"games": self.fuzz.games, "steps": self.fuzz.steps,
                         "ended": self.fuzz.ended},
                "counterexample": self.counterexample}


def verify(ref: Any, cand: Any, recorded: list[GameRecord], *, fuzz_games: int = DEFAULT_MIN_FUZZ,
           min_fuzz_games: int = DEFAULT_MIN_FUZZ, seed: int = 0,
           extra_scripts: list[tuple[int, dict]] | None = None) -> Verdict:
    """Accepted only if every recorded trace replays and `min_fuzz_games` fuzzed games match."""
    replay = replay_traces(cand, recorded)
    if replay.divergences:
        first = replay.divergences[0]
        return Verdict("Blocked", f"replay failed on {first['game']} at step {first['step']} "
                       f"({first['what']})", replay, FuzzResult(),
                       {"source": "replay", **json.loads(json.dumps(first, default=str))})
    pair = SideBySide(ref, cand)
    fuzzed = fuzz(pair, games=fuzz_games, seed=seed, extra_scripts=extra_scripts)
    if fuzzed.divergence:
        if fuzzed.divergence.kind == "crash":
            try:
                pair.restart_candidate()
            except BinaryError:
                pass
        actions, final = shrink(pair, fuzzed.players, fuzzed.script, fuzzed.divergence.actions,
                                fuzzed.divergence)
        counterexample = {
            "source": "fuzz", "players": fuzzed.players, "script": fuzzed.script,
            "actions": actions, "steps": len(actions), "kind": final.kind,
            "reference": json.loads(json.dumps(final.ref, default=str)),
            "candidate": json.loads(json.dumps(final.cand, default=str)),
            "shrunk_from": len(fuzzed.divergence.actions)}
        trace = pair.play(fuzzed.players, fuzzed.script, ListChooser(actions),
                          max_steps=len(actions) + 1, keep_trace=True)
        counterexample["reference_trace"] = trace.ref_trace
        counterexample["candidate_error"] = _candidate_error(cand, fuzzed.players, fuzzed.script, actions)
        return Verdict("Blocked", f"divergence after {fuzzed.games} games; shortest game has "
                       f"{len(actions)} action(s): {final.brief()}", replay, fuzzed, counterexample)
    if fuzzed.games < min_fuzz_games:
        return Verdict("Blocked", f"only {fuzzed.games} fuzzed games ran; {min_fuzz_games} required",
                       replay, fuzzed)
    return Verdict("Accepted", f"{replay.steps} recorded steps and {fuzzed.games} fuzzed games "
                   f"({fuzzed.steps} steps): 0 divergences", replay, fuzzed)
