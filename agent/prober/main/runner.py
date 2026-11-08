"""Play games against the binary and record every step as a trace.

Predict-then-check: before each action the predictor's guess is written to the
ledger; only then does the action run. A wrong guess is a surprise, and the
prediction on file can never be edited afterwards.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from auditor.main.corpus import GameRecord, TraceStore
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from livefeed.main.events import EventLog, safe_emit
from sandbox.main.rule_worker import RuleError, RuleWorker
from scorer.main.compare import matches

from .experiments import Experiment, build_deck

GEARS = {"gear1", "gear2", "gear3"}


class ProbeBudgetExceeded(RuntimeError):
    pass


@dataclass
class ProbeBudget:
    max_games: int | None = None
    max_steps: int | None = None
    games: int = 0
    steps: int = 0

    def start_game(self) -> None:
        if self.max_games is not None and self.games >= self.max_games:
            raise ProbeBudgetExceeded(f"game budget of {self.max_games} used up")
        self.games += 1

    def step(self) -> None:
        if self.max_steps is not None and self.steps >= self.max_steps:
            raise ProbeBudgetExceeded(f"step budget of {self.max_steps} used up")
        self.steps += 1

    def summary(self) -> dict[str, int | None]:
        return {"games": self.games, "steps": self.steps,
                "max_games": self.max_games, "max_steps": self.max_steps}


# ---------- choosers: who decides the next action ----------

Chooser = Callable[[dict[str, Any], list[dict[str, Any]], int], "dict[str, Any] | None"]


def _safe_fallback(legal: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer a harmless play; avoid gears (they push toward overflow)."""
    for action in legal:
        if action["type"] == "play" and action["card"] not in GEARS:
            return action
    return legal[0]


class ScriptedChooser:
    """Follow a list of action specs; if one is illegal, deviate safely and count it."""

    def __init__(self, specs: list[dict[str, Any]]):
        self._specs = list(specs)
        self._next = 0
        self.deviations = 0

    def __call__(self, view, legal, step_no):
        if self._next >= len(self._specs):
            return None
        wanted = self._specs[self._next]
        self._next += 1
        if wanted in legal:
            return wanted
        self.deviations += 1
        return _safe_fallback(legal)


class PolicyChooser:
    def __init__(self, name: str, seed: int = 0):
        self.name, self._rng = name, random.Random(seed)

    def __call__(self, view, legal, step_no):
        if self.name == "draw_first":
            draws = [a for a in legal if a["type"] == "draw"]
            return draws[0] if draws else _safe_fallback(legal)
        if self.name == "play_first":
            return _safe_fallback(legal)
        return self._rng.choice(legal)


def chooser_for(exp: Experiment, seed: int = 0) -> Chooser:
    if exp.actions:
        return ScriptedChooser([a.as_action() for a in exp.actions])
    return PolicyChooser(exp.policy or "random", seed)


# ---------- predictors ----------

class Predictor(Protocol):
    def start_game(self) -> None: ...
    def predict(self, step: dict[str, Any]) -> dict[str, Any]: ...
    def check(self, predictions: dict[str, Any], step: dict[str, Any]) -> list[str]: ...
    def observe(self, step: dict[str, Any]) -> None: ...


class RulePredictor:
    """Predict with every executable rule the ledger currently believes (inferred/confirmed).

    Live prediction has no outcome yet, so a rule's `prior_events` is empty. A rule
    that needs the other events of the same step will surprise; that is information
    about the rule, and the Scorer (which has full traces) stays the judge.
    """

    def __init__(self, ledger: Ledger, statuses=(Status.INFERRED, Status.CONFIRMED)):
        self._workers: dict[str, tuple[RuleWorker, dict[str, Any]]] = {}
        for claim in ledger.all_claims():
            rule = claim.rule or {}
            if claim.source is not Source.MODEL or rule.get("type") != "executable" \
                    or claim.status not in statuses:
                continue
            worker = RuleWorker()
            try:
                meta = worker.load(rule["code"])
            except RuleError:
                worker.close()
                continue
            if meta["kind"] == "setup":
                worker.close()
                continue
            self._workers[claim.id] = (worker, meta)

    @property
    def rule_ids(self) -> list[str]:
        return list(self._workers)

    def start_game(self) -> None:
        for worker, _ in self._workers.values():
            worker.live_reset()

    def predict(self, step: dict[str, Any]) -> dict[str, Any]:
        payload = {k: step[k] for k in ("before", "action", "legal", "seat") if k in step}
        out = {}
        for claim_id, (worker, _) in self._workers.items():
            result = worker.live_predict(payload)
            if "p" in result and result["p"] is not None:
                out[claim_id] = result["p"]
        return out

    def check(self, predictions: dict[str, Any], step: dict[str, Any]) -> list[str]:
        wrong = []
        for claim_id, predicted in predictions.items():
            _, meta = self._workers[claim_id]
            if not matches(meta["kind"], meta["covers"], predicted, step):
                wrong.append(claim_id)
        return wrong

    def observe(self, step: dict[str, Any]) -> None:
        for worker, _ in self._workers.values():
            worker.live_observe(step)

    def close(self) -> None:
        for worker, _ in self._workers.values():
            worker.close()


# ---------- the runner ----------

@dataclass
class GameRunner:
    box: Any                                   # BinaryProcess or any client with the contract
    store: TraceStore
    ledger: Ledger | None = None
    predictor: Predictor | None = None
    run_id: str = "r-01"
    budget: ProbeBudget = field(default_factory=ProbeBudget)
    save_path: Path | None = None
    events: EventLog | None = None

    def run(self, *, players: int, script: dict[str, Any], chooser: Chooser, label: str,
            split: str = "train", max_steps: int = 200,
            meta: dict[str, Any] | None = None) -> GameRecord:
        self.budget.start_game()
        game_key = self.store.next_game_id()
        started = self.box.new_game(players, script)
        binary_game, view = started["game_id"], started["view"]
        game = GameRecord(game_id=game_key, players=players, script=script, initial_view=view,
                          split=split, label=label, steps=[], origin="probe",
                          meta={**(meta or {}), "binary_game_id": binary_game})
        if self.predictor:
            self.predictor.start_game()
        for number in range(1, max_steps + 1):
            if view["over"]:
                break
            legal = self.box.legal_actions(binary_game)
            action = chooser(view, legal, number)
            if action is None:
                break
            self.budget.step()
            trace_id = self.store.next_trace_id()
            guesses, record = {}, None
            if self.predictor and self.ledger:
                guesses = self.predictor.predict({"before": view, "action": action, "legal": legal,
                                                  "seat": view["current_seat"]})
                if guesses:                       # logged BEFORE the action runs
                    record = self.ledger.log_prediction(
                        game_id=game_key, step=number, prediction={"rules": guesses},
                        claim_ids=list(guesses))
            result = self.box.act(binary_game, action)
            step = {"trace_id": trace_id, "run_id": self.run_id, "game_id": game_key,
                    "step": number, "seat": view["current_seat"], "turn": view["turn"],
                    "action": action, "legal": legal, "events": result["events"],
                    "view": result["view"], "split": split, "label": label,
                    "prediction": guesses or None, "surprise": None, "before": view}
            if record:
                wrong = self.predictor.check(guesses, step)
                step["surprise"] = bool(wrong)
                self.ledger.record_outcome(record.id, {"mismatched": wrong}, surprise=bool(wrong),
                                           trace_id=trace_id)
            if self.predictor:
                self.predictor.observe(step)
            game.steps.append(step)
            view = result["view"]
        deviations = getattr(chooser, "deviations", None)
        if deviations is not None:
            game.meta["deviations"] = deviations
        self.store.add_game(game)
        if self.save_path:
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            with self.save_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(game.to_json()) + "\n")
        safe_emit(self.events, "probe",
                  {"label": label, "title": (meta or {}).get("title"), "split": split,
                   "experiment": (meta or {}).get("experiment_id"),
                   "steps": len(game.steps), "predicted": bool(self.predictor),
                   "surprises": sum(1 for s in game.steps if s.get("surprise"))},
                  trace_ids=[game.steps[0]["trace_id"], game.steps[-1]["trace_id"]] if game.steps else [])
        return game

    def run_experiment(self, exp: Experiment, hand_size: int, seed: int = 0) -> GameRecord:
        chooser = chooser_for(exp, seed)
        return self.run(
            players=exp.players, script={"deck": build_deck(exp, hand_size)}, chooser=chooser,
            label=f"experiment:{exp.id}", split=exp.split, max_steps=exp.max_steps,
            meta={"experiment_id": exp.id, "title": exp.title, "topics": exp.claim_topics})

    def run_random(self, seed: int, players: int, policy: str = "random",
                   split: str = "train", max_steps: int = 200) -> GameRecord:
        script = self.box.random_script(seed)
        return self.run(players=players, script=script, chooser=PolicyChooser(policy, seed),
                        label=f"probe:{policy}:{seed}", split=split, max_steps=max_steps,
                        meta={"seed": seed})
