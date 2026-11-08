"""Run the seed experiments against the binary and record the traces.

    python -m prober.main.probe --run-id r-01 [--only e-jam-draw,e-stalemate]

New games go to runs/<run_id>/probe_games.jsonl. Experiments already run are skipped.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from auditor.main.corpus import GameRecord, TraceStore, load_games, load_seed_corpus
from ledger.main.models import Status
from ledger.main.store import Ledger
from livefeed.main.events import EventLog
from sandbox.main.binary import BinaryProcess
from sandbox.main.digsite_fs import DigSiteFS

from .experiments import Experiment, seed_experiments
from .runner import GameRunner, ProbeBudget

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent


def known_cards(store: TraceStore) -> set[str]:
    return {card for game in store.games() for card in game.script["deck"]}


def hand_size_for(ledger: Ledger, store: TraceStore) -> int:
    """The measured deal size if the audit found one, else read it off any recorded game."""
    for claim in ledger.all_claims():
        rule = claim.rule or {}
        if rule.get("type") == "deal_size" and claim.status in (Status.INFERRED, Status.OBSERVED,
                                                                 Status.CONFIRMED):
            return int(rule["value"])
    return len(store.games()[0].initial_view["hands"][0])


def experiments_done(store: TraceStore) -> set[str]:
    return {g.meta["experiment_id"] for g in store.games() if g.meta.get("experiment_id")}


def run_seed_probes(runner: GameRunner, hand_size: int, only: set[str] | None = None,
                    experiments: list[Experiment] | None = None) -> list[GameRecord]:
    done = experiments_done(runner.store)
    games = []
    for exp in experiments or seed_experiments(hand_size):
        if exp.id in done or (only and exp.id not in only):
            continue
        games.append(runner.run_experiment(exp, hand_size))
    return games


def run_random_probes(runner: GameRunner, count: int, split: str = "train",
                      first_seed: int = 1000, max_steps: int = 250) -> list[GameRecord]:
    """Extra games with varied players and policies: more evidence for rare events."""
    policies = ["random", "draw_first", "play_first", "random"]
    games = []
    for n in range(count):
        games.append(runner.run_random(seed=first_seed + n, players=2 + n % 3,
                                       policy=policies[n % len(policies)], split=split,
                                       max_steps=max_steps))
    return games


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run seed experiments against the binary.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--only", default="")
    parser.add_argument("--max-games", type=int, default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.runs_dir) / args.run_id
    fs = DigSiteFS(args.dig_site)
    store = TraceStore(load_seed_corpus(fs))
    for game in load_games(run_dir / "probe_games.jsonl"):
        store.add_game(game)
    ledger = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)
    only = {x for x in args.only.split(",") if x} or None
    with BinaryProcess(fs) as box:
        runner = GameRunner(box, store, ledger=ledger, run_id=args.run_id,
                            budget=ProbeBudget(max_games=args.max_games),
                            save_path=run_dir / "probe_games.jsonl", events=EventLog(run_dir))
        games = run_seed_probes(runner, hand_size_for(ledger, store), only)
    for game in games:
        print(f"{game.meta['experiment_id']:16} steps={len(game.steps):3} "
              f"deviations={game.meta.get('deviations', 0)} over={game.steps[-1]['view']['over']}")
    print(f"budget: {runner.budget.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
