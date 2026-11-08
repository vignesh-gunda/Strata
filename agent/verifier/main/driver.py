"""Drive the original binary and a candidate rebuild side by side and compare every reply.

Both are JSON-lines programs speaking the same contract, so one client type drives
both. The Verifier never calls a model and never looks at the manual or the code.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from sandbox.main.binary import BinaryError, BinaryRequestError

CARD_NAMES = ["gear1", "gear2", "gear3", "vent", "reverse", "double", "jam", "scan"]
Chooser = Callable[[dict[str, Any], list[dict[str, Any]], int], "dict[str, Any] | None"]


@dataclass
class Divergence:
    step: int                      # 0 = new_game, then 1.. per action
    kind: str                      # new_game | legal | act | illegal | crash
    ref: Any
    cand: Any
    actions: list[dict[str, Any]] = field(default_factory=list)   # actions played so far

    def brief(self) -> str:
        return (f"step {self.step} ({self.kind}): reference={_short(self.ref)} "
                f"candidate={_short(self.cand)}")


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass
class GameOutcome:
    players: int
    script: dict[str, Any]
    actions: list[dict[str, Any]]
    steps: int
    divergence: Divergence | None = None
    ref_trace: list[dict[str, Any]] = field(default_factory=list)   # what the original did


class Side:
    """One program plus the id of the game currently running on it."""

    def __init__(self, client: Any):
        self.client = client
        self.game_id: str | None = None

    def call(self, name: str, *args: Any) -> tuple[str, Any]:
        try:
            return "ok", getattr(self.client, name)(*args)
        except BinaryRequestError as error:
            return "error", error.kind
        except BinaryError as error:
            return "crash", f"{type(error).__name__}: {error}"

    def restart(self) -> None:
        self.client.restart()


def _normalize_new_game(reply: tuple[str, Any]) -> tuple[str, Any]:
    status, value = reply
    if status == "ok":
        return status, {"view": value["view"]}
    return reply


class SideBySide:
    def __init__(self, ref: Any, cand: Any):
        self.ref, self.cand = Side(ref), Side(cand)

    def restart_candidate(self) -> None:
        self.cand.restart()

    def play(self, players: int, script: dict[str, Any], chooser: Chooser, *,
             max_steps: int = 400, illegal_rate: float = 0.0,
             rng: random.Random | None = None, keep_trace: bool = False) -> GameOutcome:
        outcome = GameOutcome(players, script, [], 0)
        rng = rng or random.Random(0)
        r = self.ref.call("new_game", players, script)
        c = self.cand.call("new_game", players, script)
        if _normalize_new_game(r) != _normalize_new_game(c):
            outcome.divergence = Divergence(0, "crash" if "crash" in (r[0], c[0]) else "new_game",
                                            r[1], c[1], [])
            return outcome
        if r[0] != "ok":
            return outcome                      # both rejected the game the same way
        self.ref.game_id, self.cand.game_id = r[1]["game_id"], c[1]["game_id"]
        view = r[1]["view"]

        for number in range(1, max_steps + 1):
            if view["over"]:
                break
            lr = self.ref.call("legal_actions", self.ref.game_id)
            lc = self.cand.call("legal_actions", self.cand.game_id)
            if lr != lc:
                outcome.divergence = Divergence(number, "crash" if "crash" in (lr[0], lc[0]) else "legal",
                                                lr[1], lc[1], list(outcome.actions))
                return outcome
            legal = lr[1]
            if not legal:
                break
            if illegal_rate and rng.random() < illegal_rate:
                bogus = self._illegal_action(legal, rng)
                if bogus is not None:
                    ir = self.ref.call("act", self.ref.game_id, bogus)
                    ic = self.cand.call("act", self.cand.game_id, bogus)
                    if ir != ic:
                        outcome.divergence = Divergence(number, "illegal", [bogus, ir[1]],
                                                        [bogus, ic[1]], list(outcome.actions))
                        return outcome
            action = chooser(view, legal, number)
            if action is None:
                break
            if action not in legal:
                outcome.steps = -1              # the chosen action is not legal here: invalid attempt
                return outcome
            ar = self.ref.call("act", self.ref.game_id, action)
            ac = self.cand.call("act", self.cand.game_id, action)
            outcome.actions.append(action)
            outcome.steps = number
            if ar != ac:
                outcome.divergence = Divergence(number, "crash" if "crash" in (ar[0], ac[0]) else "act",
                                                ar[1], ac[1], list(outcome.actions))
                if keep_trace and ar[0] == "ok":        # show the step where they part ways
                    outcome.ref_trace.append({"step": number, "action": action,
                                              "events": ar[1]["events"], "view": ar[1]["view"]})
                return outcome
            if ar[0] != "ok":
                break
            if keep_trace:
                outcome.ref_trace.append({"step": number, "action": action,
                                          "events": ar[1]["events"], "view": ar[1]["view"]})
            view = ar[1]["view"]
        return outcome

    @staticmethod
    def _illegal_action(legal: list[dict[str, Any]], rng: random.Random) -> dict[str, Any] | None:
        playable = {a["card"] for a in legal if a["type"] == "play"}
        absent = [c for c in CARD_NAMES if c not in playable]
        options: list[dict[str, Any]] = [{"type": "play", "card": c} for c in absent]
        if not any(a["type"] == "draw" for a in legal):
            options.append({"type": "draw"})
        if not any(a["type"] == "pass" for a in legal):
            options.append({"type": "pass"})
        return rng.choice(options) if options else None


# ---------- choosers ----------

class FuzzChooser:
    """Random legal actions, in four flavors so games reach different endings."""

    def __init__(self, rng: random.Random, mode: int):
        self.rng, self.mode = rng, mode % 4

    def __call__(self, view, legal, step):
        if self.mode == 1:                       # draw whenever possible: long games, full hands
            draws = [a for a in legal if a["type"] == "draw"]
            if draws:
                return draws[0]
        if self.mode == 2:                       # avoid gears: reach stalemates, not overflows
            calm = [a for a in legal if a["type"] != "play" or not a["card"].startswith("gear")]
            if calm:
                return self.rng.choice(calm)
        if self.mode == 3:                       # play when possible
            plays = [a for a in legal if a["type"] == "play"]
            if plays:
                return self.rng.choice(plays)
        return self.rng.choice(legal)


class ListChooser:
    def __init__(self, actions: list[dict[str, Any]]):
        self._actions, self._i = list(actions), 0

    def __call__(self, view, legal, step):
        if self._i >= len(self._actions):
            return None
        self._i += 1
        return self._actions[self._i - 1]
