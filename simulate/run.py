"""Simulate many games against the black box and record every step.

    python -m simulate.run --games 400 --seed 42 --mutations 2 --run-id r-01
    python -m simulate.run --forever --live --delay 0.05     # nonstop, every step printed


Writes traces/<run_id>/{traces,games}.jsonl and manifest.json. Every game is
reproducible from its script. About 1 game in 5 is a targeted scenario and
1 in 5 is marked ``heldout`` for scoring. The planted mutations are never
written unless ``--sealed-out`` is given (for the final report only).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from blackbox.client import BlackBoxClient
from simulate.live import format_game, format_trace
from simulate.policies import POLICIES
from simulate.recorder import TraceRecorder
from simulate.scenarios import TEMPLATES, arrange

MAX_STEPS = 2_000
TEMPLATE_NAMES = tuple(TEMPLATES)
POLICY_NAMES = tuple(POLICIES)


@dataclass(frozen=True)
class RunConfig:
    games: int = 400
    seed: int = 42
    mutations: int = 2
    opaque: bool = False
    run_id: str = "r-01"
    out: Path = Path("traces")
    sealed_out: Path | None = None
    forever: bool = False  # keep simulating until stopped (Ctrl+C)
    live: bool = False  # print every step to the terminal
    delay: float = 0.0  # seconds to wait after each step, so a live feed is readable

    def __post_init__(self) -> None:
        if not 1 <= self.games <= 100_000:
            raise ValueError("games must be 1-100000")
        if not 0 <= self.mutations <= 6:
            raise ValueError("mutations must be 0-6")
        if not 0 <= self.delay <= 5:
            raise ValueError("delay must be 0-5 seconds")
        if not self.run_id.replace("-", "").isalnum():
            raise ValueError("run_id may only contain letters, digits and dashes")


@dataclass(frozen=True)
class GameSpec:
    index: int
    label: str  # "scenario:<name>" or "policy:<name>"
    players: int
    policy: str
    script_seed: int
    split: str  # "train" or "heldout"


def spec_for(config: RunConfig, index: int) -> GameSpec:
    """The game at position ``index``: 1 in 5 is a scenario, 1 in 5 is held out."""
    split = "heldout" if index % 10 in (5, 9) else "train"
    script_seed = config.seed * 100_003 + index
    if index % 5 == 0 and not config.opaque:
        name = TEMPLATE_NAMES[(index // 5) % len(TEMPLATE_NAMES)]
        policy = POLICY_NAMES[index % len(POLICY_NAMES)]
        return GameSpec(index, f"scenario:{name}", TEMPLATES[name].players, policy, script_seed, split)
    played = index if config.opaque else index - (index + 4) // 5  # policy games before this one
    policy = "random" if config.opaque else POLICY_NAMES[played % len(POLICY_NAMES)]
    return GameSpec(index, f"policy:{policy}", 2 + index % 3, policy, script_seed, split)


def build_specs(config: RunConfig) -> list[GameSpec]:
    return [spec_for(config, index) for index in range(config.games)]


def iter_specs(config: RunConfig) -> Iterator[GameSpec]:
    index = 0
    while config.forever or index < config.games:
        yield spec_for(config, index)
        index += 1


def _script_for(box: BlackBoxClient, spec: GameSpec) -> dict:
    deck = box.random_script(spec.script_seed)["deck"]
    if not spec.label.startswith("scenario:"):
        return {"deck": deck}
    template = TEMPLATES[spec.label.split(":", 1)[1]]
    return {"deck": arrange(deck, spec.players, template.hands, template.pile_top)}


def _opening(spec: GameSpec) -> tuple[dict, ...]:
    return TEMPLATES[spec.label.split(":", 1)[1]].opening if spec.label.startswith("scenario:") else ()


def play_game(box: BlackBoxClient, recorder: TraceRecorder, spec: GameSpec,
              on_trace: Callable[[dict], None], delay: float = 0.0,
              on_start: Callable[[dict], None] | None = None) -> dict:
    script = _script_for(box, spec)
    game = box.new_game(spec.players, script)
    game_id, view = game["game_id"], game["view"]
    if on_start is not None:
        on_start({"game_id": game_id, "index": spec.index, "label": spec.label, "split": spec.split,
                  "players": spec.players, "view": view})
    rng = random.Random(spec.script_seed)
    opening, broken, step = list(_opening(spec)), False, 0
    while step < MAX_STEPS:
        legal = box.legal_actions(game_id)
        if not legal:
            break
        scripted = opening.pop(0) if opening and not broken else None
        broken = broken or (scripted is not None and scripted not in legal)
        action = scripted if scripted in legal else POLICIES[spec.policy](legal, view, rng)
        seat, turn = view["current_seat"], view["turn"]
        result = box.act(game_id, action)
        step += 1
        on_trace(recorder.trace(
            {"game_id": game_id, "step": step, "seat": seat, "turn": turn, "action": action, "legal": legal,
             "events": result["events"], "view": result["view"], "split": spec.split, "label": spec.label}
        ))
        view = result["view"]
        if delay:
            time.sleep(delay)
    return recorder.game(
        {"game_id": game_id, "index": spec.index, "label": spec.label, "split": spec.split, "players": spec.players,
         "script": script, "initial_view": game["view"], "steps": step, "winner": view["winner"],
         "opening_broken": broken}
    )


def _manifest(config: RunConfig, recorder: TraceRecorder, games: int, heldout: int, status: str) -> dict:
    return {"run_id": config.run_id, "updated_at": datetime.now(timezone.utc).isoformat(), "status": status,
            "games": games, "traces": recorder.trace_count, "heldout_games": heldout, "opaque": config.opaque,
            "mutations": "hidden", "black_box_seed": config.seed}


def _print(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def run(config: RunConfig) -> Path:
    run_dir = config.out / config.run_id
    box_args = {"seed": config.seed, "mutations": config.mutations, "opaque": config.opaque}
    show = (lambda trace: _print(format_trace(trace))) if config.live else (lambda trace: None)
    games = heldout = 0
    with BlackBoxClient(**box_args, allow_reveal=config.sealed_out is not None) as box:
        with TraceRecorder(run_dir, config.run_id) as recorder:
            status = "running"
            try:
                for spec in iter_specs(config):
                    game = play_game(box, recorder, spec, show, config.delay)
                    games, heldout = games + 1, heldout + (spec.split == "heldout")
                    if config.live:
                        _print(format_game(game, {"games": games, "traces": recorder.trace_count}))
                    if games % 10 == 0:
                        recorder.manifest(_manifest(config, recorder, games, heldout, status))
                status = "complete"
            except (KeyboardInterrupt, BrokenPipeError):  # Ctrl+C, or the viewer (e.g. `| head`) went away
                status = "stopped"
            recorder.manifest(_manifest(config, recorder, games, heldout, status))
        if config.sealed_out is not None:
            config.sealed_out.parent.mkdir(parents=True, exist_ok=True)
            config.sealed_out.write_text(json.dumps(box.reveal(), indent=2) + "\n", encoding="utf-8")
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulate games and record evidence traces")
    parser.add_argument("--games", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42, help="black-box run seed (picks hidden mutations)")
    parser.add_argument("--mutations", type=int, default=2)
    parser.add_argument("--opaque", action="store_true")
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--out", type=Path, default=Path("traces"))
    parser.add_argument("--sealed-out", type=Path, default=None, help="write planted mutations here (report only)")
    parser.add_argument("--forever", action="store_true", help="keep simulating until Ctrl+C")
    parser.add_argument("--live", action="store_true", help="print every step as it happens")
    parser.add_argument("--delay", type=float, default=0.0, help="pause after each step (seconds)")
    args = parser.parse_args(argv)
    config = RunConfig(args.games, args.seed, args.mutations, args.opaque, args.run_id, args.out, args.sealed_out,
                       args.forever, args.live, args.delay)
    run_dir = run(config)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    try:
        _print(f"{manifest['status']}: {manifest['games']} games, {manifest['traces']} traces -> {run_dir}")
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")  # reader closed early; the files are already complete
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
