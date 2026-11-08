"""Write runs/<run_id>/report.md and report.json.

    python -m report.main.cli --run-id r-01 [--no-reveal]

Exit codes: 0 written, 1 refused because a claim cites no real trace, 2 no ledger.
The mutation check runs only if the run has finished; otherwise that section says so.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from auditor.main.corpus import TraceStore, load_games, load_seed_corpus
from ledger.main.store import Ledger
from sandbox.main.digsite_fs import DigSiteFS

from .build import ReportError, build_report
from .reveal import RevealRefused, check_mutations

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent
DEFAULT_SEALED = REPO_ROOT / "services" / "pressure" / "sealed" / "runs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the run report.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--sealed-dir", default=str(DEFAULT_SEALED))
    parser.add_argument("--no-reveal", action="store_true", help="leave the mutation check sealed")
    args = parser.parse_args(argv)

    run_dir = Path(args.runs_dir) / args.run_id
    if not (run_dir / "ledger.jsonl").exists():
        print(f"No ledger for run {args.run_id}.")
        return 2
    fs = DigSiteFS(args.dig_site)
    store = TraceStore(load_seed_corpus(fs))
    for game in load_games(run_dir / "probe_games.jsonl"):
        store.add_game(game)
    ledger = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)

    rows, refused = None, "left sealed by request" if args.no_reveal else None
    if not args.no_reveal:
        try:
            rows = check_mutations(run_dir, ledger, Path(args.sealed_dir), str(fs.manifest().get("run_id", "")))
        except RevealRefused as why:
            refused = str(why)
    try:
        report = build_report(run_dir, ledger, store.exists, mutation_rows=rows, mutation_refused=refused)
    except ReportError as error:
        print(error)
        return 1
    (run_dir / "report.md").write_text(report.markdown)
    (run_dir / "report.json").write_text(json.dumps(report.data, indent=2))
    print(f"Wrote {run_dir / 'report.md'}" + (f" (mutation check: {refused})" if refused else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
