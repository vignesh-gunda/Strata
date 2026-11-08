"""Verify a rebuild against the original binary.

    python -m verifier.main.cli --candidate "node /path/to/server.js" --run-id r-01 --fuzz 5000

Prints Accepted or Blocked (with the shortest counterexample) and writes
runs/<run_id>/verdict.json. No model is involved in the decision.
"""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

from auditor.main.corpus import load_games, load_seed_corpus
from sandbox.main.binary import BinaryProcess
from sandbox.main.digsite_fs import DigSiteFS

from .checks import DEFAULT_MIN_FUZZ, verify

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent


def run_verification(fs: DigSiteFS, candidate_command: list[str], run_dir: Path, *,
                     fuzz_games: int, seed: int = 0):
    recorded = load_seed_corpus(fs) + load_games(run_dir / "probe_games.jsonl")
    extra = [(g.players, g.script) for g in recorded]            # real decks first
    with BinaryProcess(fs) as ref, BinaryProcess(command=candidate_command, timeout=20) as cand:
        return verify(ref, cand, recorded, fuzz_games=fuzz_games,
                      min_fuzz_games=DEFAULT_MIN_FUZZ, seed=seed, extra_scripts=extra)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a rebuild against the binary.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--candidate", required=True, help="command that starts the rebuild")
    parser.add_argument("--fuzz", type=int, default=DEFAULT_MIN_FUZZ)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    run_dir = Path(args.runs_dir) / args.run_id
    verdict = run_verification(DigSiteFS(args.dig_site), shlex.split(args.candidate), run_dir,
                               fuzz_games=args.fuzz, seed=args.seed)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verdict.json").write_text(json.dumps(verdict.to_json(), indent=2))
    print(f"{verdict.verdict.upper()}: {verdict.reason}")
    if verdict.counterexample:
        print(json.dumps({k: verdict.counterexample[k] for k in ("actions", "kind", "reference", "candidate")
                          if k in verdict.counterexample}, indent=1)[:1500])
    return 0 if verdict.verdict == "Accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
