"""Survey: read every artifact, play no game, produce a starting ledger and a probe list.

    python -m surveyor.main.survey --run-id r-01 [--use-muse]

Writes runs/<run_id>/{ledger.jsonl, probe_list.json, survey.json}.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledger.main.models import Source
from livefeed.main.events import EventLog, safe_emit
from ledger.main.store import Ledger
from sandbox.main.digsite_fs import DigSiteFS
from sandbox.main.metadata import binary_metadata

from .code import HINT, survey_code
from .manual import parse_manual
from .probes import build_probe_list

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent
CREATED_BY = "surveyor"
MANUAL_FALLBACK = "docs/pressure-manual-v1.md"
CODE_FALLBACK = ["source/engine.py", "source/state.py"]
CORPUS = "traces/seed-corpus/traces.jsonl"


@dataclass
class SurveyResult:
    probe_list: list[dict[str, Any]]
    summary: dict[str, Any]


def _artifact_paths(fs: DigSiteFS) -> tuple[str, list[str]]:
    """Find the manual and the code files through the manifest."""
    manual, code = MANUAL_FALLBACK, list(CODE_FALLBACK)
    try:
        for artifact in fs.manifest().get("artifacts", []):
            paths = [f["path"] for f in artifact.get("files", [])]
            if artifact.get("kind") == "documentation" and paths:
                manual = paths[0]
            elif artifact.get("kind") == "partial_code" and paths:
                code = [p for p in paths if p.endswith(".py")]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return manual, code


def _view_keys(fs: DigSiteFS) -> set[str] | None:
    """Schema only: the keys of the first corpus view. No claim is checked here."""
    try:
        first = next(fs.iter_jsonl(CORPUS))
        return set(first["view"])
    except (FileNotFoundError, StopIteration, KeyError, PermissionError):
        return None


def run_survey(fs: DigSiteFS, ledger: Ledger, *, run_dir: Path | None = None,
               muse: Any = None, run_id: str = "r-01",
               events: EventLog | None = None) -> SurveyResult:
    manual_path, code_paths = _artifact_paths(fs)
    manual_text = fs.read_text(manual_path)
    code_files = {p: fs.read_text(p) for p in code_paths}

    # 1. Documented-tier claims from the manual.
    for spec in parse_manual(manual_text):
        ledger.add_claim(text=spec.text, source=Source.MANUAL, created_by=CREATED_BY,
                         key=f"manual:{spec.key}", topic=spec.topic, rule=spec.rule,
                         kind=spec.kind, subkind=spec.subkind,
                         notes=[f"from {manual_path}"] + spec.notes)

    # 2. Read-from-code claims, plus the unknowns the code reveals.
    code = survey_code(code_files, view_keys=_view_keys(fs))
    for spec, hint_only in code.claims:
        ledger.add_claim(text=spec.text, source=Source.CODE, created_by=CREATED_BY,
                         key=f"code:{spec.key}", topic=spec.topic, rule=spec.rule,
                         kind=spec.kind, subkind=HINT if hint_only else spec.subkind,
                         notes=["from the recovered source"] + spec.notes)
    for spec in code.unknown_specs():
        ledger.add_claim(text=spec.text, source=Source.CODE, created_by=CREATED_BY,
                         key=f"code:{spec.key}", topic=spec.topic, rule=spec.rule,
                         kind=spec.kind, subkind=spec.subkind,
                         notes=["from the recovered source"] + spec.notes)

    # 3. Optional Muse pass for claims the parsers missed (OPEN, created_by muse).
    muse_added: list[str] = []
    if muse is not None:
        from .muse_extra import muse_extra_claims
        muse_added += muse_extra_claims(muse, ledger, artifact_text=manual_text,
                                        artifact_path=manual_path, source=Source.MANUAL)
        for path, text in code_files.items():
            muse_added += muse_extra_claims(muse, ledger, artifact_text=text,
                                            artifact_path=path, source=Source.CODE)

    # 4. Binary: metadata only (hash, size, entry point). The binary is not run here.
    try:
        binary = binary_metadata(fs)
    except FileNotFoundError:
        binary = None

    probes = build_probe_list(ledger)
    summary = {
        "run_id": run_id,
        "artifacts": {"manual": manual_path, "code": code_paths},
        "binary": binary,
        "counts": ledger.counts(),
        "claims": len(ledger.all_claims()),
        "unknowns": {
            "missing_functions": code.missing_functions,
            "missing_modules": code.missing_modules,
            "parameters": code.parameters,
            "hidden_state": code.hidden_state,
        },
        "muse_added": muse_added,
        "probe_count": len(probes),
        "games_played": 0,
    }
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "probe_list.json").write_text(json.dumps(probes, indent=2))
        (run_dir / "survey.json").write_text(json.dumps(summary, indent=2))
    safe_emit(events, "survey", {"claims": summary["claims"], "probes": len(probes),
                                "unknowns": sum(1 for c in ledger.all_claims() if c.kind == "unknown")})
    return SurveyResult(probe_list=probes, summary=summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Survey the dig-site (plays no game).")
    parser.add_argument("--dig-site", default=str(REPO_ROOT / "dig-site"))
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--use-muse", action="store_true",
                        help="also ask Muse for claims the parsers missed")
    args = parser.parse_args(argv)

    run_dir = Path(args.runs_dir) / args.run_id
    fs = DigSiteFS(args.dig_site)
    ledger = Ledger(run_dir / "ledger.jsonl")
    muse = None
    if args.use_muse:
        from llm.main.muse import MuseClient
        muse = MuseClient()
    result = run_survey(fs, ledger, run_dir=run_dir, muse=muse, run_id=args.run_id,
                        events=EventLog(run_dir))

    summary = result.summary
    print(f"Survey {args.run_id}: {summary['claims']} claims, {summary['counts']}")
    print(f"Unknowns: {json.dumps(summary['unknowns'])}")
    print("Top of the probe list:")
    for item in result.probe_list[:8]:
        print(f"  {item['rank']:>2}. [{item['class_name']}] {item['text']}")
    print(f"Wrote {run_dir}/ledger.jsonl, probe_list.json, survey.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
