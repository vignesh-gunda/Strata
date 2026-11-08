"""Build the JavaScript rebuild from Confirmed rules, verify it, repair it.

    python -m rebuilder.main.cli --run-id r-01 --attempts 4 --fuzz 5000 [--language go]

Output: <repo>/rebuild/<run_id>/<language>/program/ (engine + fixed contract server), report.json beside it,
and rebuild_report.json for the latest language. Exit codes: 0 accepted, 1 blocked,
2 no confirmed rules, 3 the language's toolchain is not installed.
Counterexamples are re-run on the original and stored as new probe traces, so the
next hypothesis round can learn from them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from auditor.main.corpus import TraceStore, load_games, load_seed_corpus
from hypothesizer.main.hypothesize import confirmed_rules
from ledger.main.store import Ledger
from livefeed.main.events import EventLog
from llm.main.muse import MuseClient
from prober.main.runner import GameRunner, ScriptedChooser
from sandbox.main.binary import BinaryProcess
from sandbox.main.digsite_fs import DigSiteFS
from verifier.main.cli import run_verification

from .feedback import apply_divergence_feedback
from .location import rebuild_dir
from .scaffold import folder_for, program_dir
from .languages import ToolchainMissing, UnsupportedLanguage, resolve
from .rebuild import Rebuilder, build_and_repair

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent


def record_counterexample(runner: GameRunner, counterexample: dict[str, Any]) -> str | None:
    """Replay the shortest failing game on the original so it becomes a citable trace.

    Returns the trace ID of its last step (the one that shows the disagreement), if one was stored.
    """
    if counterexample.get("source") != "fuzz" or not counterexample.get("script"):
        return None
    game = runner.run(players=counterexample["players"], script=counterexample["script"],
                      chooser=ScriptedChooser(counterexample["actions"]), label="counterexample",
                      split="train", max_steps=len(counterexample["actions"]) + 1,
                      meta={"experiment_id": f"ce-{runner.budget.games + 1}", "counterexample": True})
    steps = getattr(game, "steps", None) or []
    return steps[-1].get("trace_id") if steps and isinstance(steps[-1], dict) else None


def learn_from_divergence(runner: GameRunner, ledger: Ledger, rules: list[dict[str, Any]],
                          counterexample: dict[str, Any], probe_list_path: Path) -> dict[str, Any]:
    """T16.10: store the failing game as a trace, lower trust in the rules it touches, ask the question."""
    trace_id = record_counterexample(runner, counterexample)
    return apply_divergence_feedback(ledger, rules, counterexample, trace_id=trace_id,
                                     probe_list_path=probe_list_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild in JavaScript from Confirmed rules.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--fuzz", type=int, default=5000)
    parser.add_argument("--language", default="js", help="js (default) or go")
    args = parser.parse_args(argv)

    try:
        language = resolve(args.language)
        language.require_toolchain()
    except (UnsupportedLanguage, ToolchainMissing) as error:
        print(error)
        return 3

    run_dir = Path(args.runs_dir) / args.run_id
    fs = DigSiteFS(args.dig_site)
    store = TraceStore(load_seed_corpus(fs))
    for game in load_games(run_dir / "probe_games.jsonl"):
        store.add_game(game)
    ledger = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)
    rules = confirmed_rules(ledger)
    if not rules:
        print("No Confirmed rules in this run: run the hypothesizer first.")
        return 2
    lead_code = fs.read_text("source/engine.py")
    rebuilder = Rebuilder(MuseClient(timeout=120.0), store, lead_code=lead_code, language=language)

    events = EventLog(run_dir)
    with BinaryProcess(fs) as original:
        runner = GameRunner(original, store, ledger=ledger, run_id=args.run_id,
                            save_path=run_dir / "probe_games.jsonl", events=events)
        outcome = build_and_repair(
            rebuilder, rules, program_dir(run_dir, language),
            verify=lambda command: run_verification(fs, command, run_dir, fuzz_games=args.fuzz),
            max_attempts=args.attempts, language=language.name,
            on_counterexample=lambda ce: learn_from_divergence(runner, ledger, rules, ce,
                                                               run_dir / "probe_list.json"),
            events=events)
    report = {"language": language.name, "language_key": language.key, "accepted": outcome.accepted, "attempts": outcome.attempts, "history": outcome.history,
              "rules_used": [r["claim_id"] for r in rules],
              "unknown_behaviors": outcome.result.unknown_behaviors if outcome.result else None,
              "note": "repairs after a counterexample use the original's behavior, not a Confirmed rule"}
    (run_dir / "rebuild_report.json").write_text(json.dumps(report, indent=2))
    target = folder_for(run_dir, language)            # report.json sits beside program/, notes/, rules/
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text(json.dumps(report, indent=2))
    print(("ACCEPTED" if outcome.accepted else "BLOCKED"), "after", outcome.attempts, "attempt(s)")
    for entry in outcome.history:
        print(f"  attempt {entry['attempt']}: {entry.get('verdict')} {entry.get('reason', '')[:120]}")
    return 0 if outcome.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
