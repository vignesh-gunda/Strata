"""Games and traces, with the train / held-out split enforced by construction.

`FittingCorpus` is the only thing anything that *fits* (checkers, the
Hypothesizer, the Prober's designers) ever receives. It is built from train
games only, so held-out steps are not merely discouraged: they are absent.
`ScoringCorpus` has everything and goes to the Scorer alone.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from sandbox.main.digsite_fs import DigSiteFS

SEED_TRACES = "traces/seed-corpus/traces.jsonl"
SEED_GAMES = "traces/seed-corpus/games.jsonl"
SPLITS = ("train", "heldout")


@dataclass
class GameRecord:
    game_id: str
    players: int
    script: dict[str, Any]
    initial_view: dict[str, Any]
    split: str                      # train | heldout
    label: str
    steps: list[dict[str, Any]]     # each step carries "before": the view before the action
    origin: str = "corpus"          # corpus | probe
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def last(self) -> dict[str, Any] | None:
        return self.steps[-1] if self.steps else None

    def to_json(self) -> dict[str, Any]:
        steps = [{k: v for k, v in s.items() if k != "before"} for s in self.steps]
        return {"game_id": self.game_id, "players": self.players, "script": self.script,
                "initial_view": self.initial_view, "split": self.split, "label": self.label,
                "origin": self.origin, "meta": self.meta, "steps": steps}

    @staticmethod
    def from_json(data: dict[str, Any]) -> "GameRecord":
        game = GameRecord(
            game_id=data["game_id"], players=data["players"], script=data["script"],
            initial_view=data["initial_view"], split=data["split"], label=data.get("label", ""),
            steps=[dict(s) for s in data["steps"]], origin=data.get("origin", "probe"),
            meta=data.get("meta", {}))
        _add_before(game)
        return game


def _add_before(game: GameRecord) -> None:
    previous = game.initial_view
    for step in game.steps:
        step["before"] = previous
        previous = step["view"]


def load_seed_corpus(fs: DigSiteFS) -> list[GameRecord]:
    """Read the seed corpus through the dig-site jail."""
    by_game: dict[str, list[dict[str, Any]]] = {}
    for step in fs.iter_jsonl(SEED_TRACES):
        by_game.setdefault(step["game_id"], []).append(step)
    games = []
    for meta in fs.iter_jsonl(SEED_GAMES):
        steps = sorted(by_game.get(meta["game_id"], []), key=lambda s: s["step"])
        game = GameRecord(
            game_id=meta["game_id"], players=meta["players"], script=meta["script"],
            initial_view=meta["initial_view"], split=meta["split"], label=meta.get("label", ""),
            steps=steps, origin="corpus")
        _add_before(game)
        games.append(game)
    return games


def save_games(path: Path, games: list[GameRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for game in games:
            handle.write(json.dumps(game.to_json()) + "\n")


def load_games(path: Path) -> list[GameRecord]:
    if not path.exists():
        return []
    return [GameRecord.from_json(json.loads(line))
            for line in path.read_text().splitlines() if line.strip()]


class _View:
    """Shared read API."""

    def __init__(self, games: list[GameRecord]):
        self._games = games

    def games(self) -> list[GameRecord]:
        return self._games

    def steps(self) -> Iterator[tuple[GameRecord, dict[str, Any]]]:
        for game in self._games:
            for step in game.steps:
                yield game, step

    def step_count(self) -> int:
        return sum(len(g.steps) for g in self._games)


class FittingCorpus(_View):
    """Train games only. Held-out data does not exist here."""

    def __init__(self, games: list[GameRecord]):
        bad = [g.game_id for g in games if g.split != "train"]
        if bad:
            raise ValueError(f"FittingCorpus may only hold train games, got {bad[:3]}")
        super().__init__(games)


class ScoringCorpus(_View):
    """Every game, for the Scorer."""

    def by_split(self, split: str) -> list[GameRecord]:
        return [g for g in self._games if g.split == split]


class TraceStore:
    """All games from the corpus and from probes; answers 'does this trace ID exist?'."""

    def __init__(self, games: list[GameRecord] | None = None):
        self._games: list[GameRecord] = []
        self._ids: set[str] = set()
        self._probe_counter = 0
        self._game_counter = 0
        for game in games or []:
            self.add_game(game)

    def add_game(self, game: GameRecord) -> None:
        self._games.append(game)
        for step in game.steps:
            self._ids.add(step["trace_id"])
        if game.origin == "probe":
            self._game_counter = max(self._game_counter, int(game.game_id.split("-")[1]))
            for step in game.steps:
                if step["trace_id"].startswith("x-"):
                    self._probe_counter = max(self._probe_counter, int(step["trace_id"][2:]))

    def register_trace(self, trace_id: str) -> None:
        """Make a trace ID citable as soon as the step runs, before its game is saved."""
        self._ids.add(trace_id)

    def next_trace_id(self) -> str:
        self._probe_counter += 1
        trace_id = f"x-{self._probe_counter:06d}"
        self._ids.add(trace_id)
        return trace_id

    def next_game_id(self) -> str:
        self._game_counter += 1
        return f"px-{self._game_counter:04d}"

    def exists(self, trace_id: str) -> bool:
        return trace_id in self._ids

    def games(self) -> list[GameRecord]:
        return list(self._games)

    def fitting(self) -> FittingCorpus:
        return FittingCorpus([g for g in self._games if g.split == "train"])

    def scoring(self) -> ScoringCorpus:
        return ScoringCorpus(list(self._games))

    def find_step(self, trace_id: str) -> tuple[GameRecord, dict[str, Any]] | None:
        for game in self._games:
            for step in game.steps:
                if step["trace_id"] == trace_id:
                    return game, step
        return None

    def split_of(self, trace_id: str) -> str | None:
        found = self.find_step(trace_id)
        return found[0].split if found else None


def deep(value: Any) -> Any:
    return copy.deepcopy(value)
