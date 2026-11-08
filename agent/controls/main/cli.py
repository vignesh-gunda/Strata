"""Run the controls on a saved run and write runs/<run_id>/controls.json and controls.md.

    python -m controls.main.cli --run-id r-live [--curve] [--ablation] [--opaque]

No model is called and no game is played: rules and games come from the run's files.
With no flag, all three controls run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from auditor.main.corpus import load_games, load_seed_corpus
from ledger.main.store import Ledger
from prober.main.experiments import seed_experiments
from sandbox.main.digsite_fs import DigSiteFS

from .ablation import compare
from .curve import baseline_is_low, learning_curve
from .opaque import opaque_check
from .replay import rules_from_ledger

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent


def _coverage_sentence(w: dict[str, Any], o: dict[str, Any]) -> str:
    def reach(arm: dict[str, Any]) -> str:
        n = arm["probes_to_100pct_coverage"]
        return f"reaches all of them after {n} probe game(s)" if n else "never reaches all of them in the games played"
    early = [p for p in (50, 90) if (o[f"probes_to_{p}pct_coverage"] or 10**9) < (w[f"probes_to_{p}pct_coverage"] or 10**9)]
    faster = (f"; the traces-only order gets to {' and '.join(str(p) + '%' for p in early)} sooner" if early else "")
    return f"with artifacts the order {reach(w)}; traces only {reach(o)}{faster}."


def markdown(result: dict[str, Any]) -> str:
    out = [f"# Controls for run {result['run_id']}", "",
           f"{result['rules']} rules re-scored on {result['seed_games']} recorded games plus up to "
           f"{result['probe_games']} probe games. No model was called.", ""]
    if "curve" in result:
        out += ["## Learning curve over probe count (T12.3)", "",
                "| Probe games | Confirmed | Inferred | Refuted | Groups with a Confirmed rule | Held-out accuracy |",
                "|---|---|---|---|---|---|"]
        out += [f"| {r['k']} | {r['confirmed']} | {r['inferred']} | {r['refuted']} | {r['groups_with_confirmed']} "
                f"| {r['heldout_accuracy']} |" for r in result["curve"]]
        out += ["", "Zero-probe baseline scores low (nothing Confirmed, then more with probes): "
                    + ("**yes**" if result["baseline_is_low"] else "**no**"), ""]
    if "ablation" in result:
        a = result["ablation"]
        out += ["## Traces only vs traces plus artifacts (T12.1)", "",
                "The artifacts only decide which behaviors are probed first. Same rules, same scorer; only the "
                "probe order differs.", "",
                "| Arm | Probe games until the Confirmed set stops changing | Confirmed at the end |", "|---|---|---|"]
        for name in ("with_artifacts", "traces_only"):
            out.append(f"| {name.replace('_', ' ')} | {a[name]['probes_to_settle']} | {len(a[name]['final_confirmed'])} |")
        out += ["", f"Same Confirmed rules at the end: **{'yes' if a['same_confirmed_rules'] else 'no'}**. "
                    f"Fewer probes with artifacts: **{'yes' if a['fewer_probes_with_artifacts'] else 'no'}**.",
                f"Only with artifacts: {len(a['only_with_artifacts'])} rule(s). Only traces-only: "
                f"{len(a['only_traces_only'])} rule(s).", "",
                f"Confirming a rule takes one fresh probe step in either order, so that count cannot separate the arms. "
                f"A fairer measure is how fast each order exercises behaviors the recorded games never showed "
                f"({a['with_artifacts']['new_step_kinds_total']} kinds of situation):", "",
                "| Arm | Probe games to 50% | to 90% | to 100% |", "|---|---|---|---|"]
        for name in ("with_artifacts", "traces_only"):
            arm = a[name]
            out.append(f"| {name.replace('_', ' ')} | {arm['probes_to_50pct_coverage']} | "
                       f"{arm['probes_to_90pct_coverage']} | {arm['probes_to_100pct_coverage']} |")
        w, o = a["with_artifacts"], a["traces_only"]
        out += ["", "Reading: " + _coverage_sentence(w, o), ""]
    if "opaque" in result:
        o = result["opaque"]
        out += ["## Opaque tokens (T12.2)", "",
                f"{o['tokens']} card names replaced by meaningless tokens in games and rules. Same ledger: "
                f"**{'yes' if o['same_ledger'] else 'no'}** ({len(o['changed'])} of {o['rules']} rules changed status).", ""]
        out += [f"- [{c['group']}] {c['text']}: {c['before']} -> {c['after']}" for c in o["changed"][:10]]
        out.append("")
    out += ["_These replay a saved run. They show how the scoring behaves; they do not re-run the Muse "
            "proposal step, so they cannot show whether the agent would have found the same rules._", ""]
    return "\n".join(out)


def run_controls(run_dir: Path, fs: DigSiteFS, *, curve: bool, ablation: bool, opaque: bool) -> dict[str, Any]:
    ledger = Ledger(run_dir / "ledger.jsonl")
    rules = rules_from_ledger(ledger)
    seed = load_seed_corpus(fs)
    probes = load_games(run_dir / "probe_games.jsonl")
    result: dict[str, Any] = {"run_id": run_dir.name, "rules": len(rules), "seed_games": len(seed),
                              "probe_games": len(probes)}
    if curve:
        result["curve"] = learning_curve(rules, seed, probes)
        result["baseline_is_low"] = baseline_is_low(result["curve"])
    if ablation:
        ids = [e.id for e in seed_experiments()]
        result["ablation"] = compare(rules, seed, probes, ids, [0, 1, 2, 3, 5, 8, 11, 15, 20, 30, len(probes)])
    if opaque:
        result["opaque"] = opaque_check(rules, seed + probes)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Controls on a saved run.")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--curve", action="store_true")
    parser.add_argument("--ablation", action="store_true")
    parser.add_argument("--opaque", action="store_true")
    args = parser.parse_args(argv)
    run_dir = Path(args.runs_dir) / args.run_id
    if not (run_dir / "ledger.jsonl").exists():
        print(f"No ledger for run {args.run_id}.")
        return 2
    chosen = args.curve or args.ablation or args.opaque
    result = run_controls(run_dir, DigSiteFS(args.dig_site), curve=args.curve or not chosen,
                          ablation=args.ablation or not chosen, opaque=args.opaque or not chosen)
    (run_dir / "controls.json").write_text(json.dumps(result, indent=2))
    (run_dir / "controls.md").write_text(markdown(result))
    print(markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
