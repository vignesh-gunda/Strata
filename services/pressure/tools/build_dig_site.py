"""Build the dig-site: the incomplete evidence Strata's agents start from.

    python -m tools.build_dig_site --seed 42 --mutations 2 --games 40 --run-id r-01

Writes four artifacts into ``dig-site/`` (at the repo root by default):

    bin/pressure.pyz         binary: the runnable game, mutations baked in
    traces/seed-corpus/      runtime traces recorded from the same build
    source/                  partial code: engine.py with 6 functions removed, state.py
    docs/                    documentation: an old manual that is out of date

plus ``MANIFEST.json``. The planted mutations go to ``sealed/runs/<run_id>.json``
inside this service, never into the dig-site.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import py_compile
import random
import re
import shutil
import sys
import tempfile
import zipapp
from datetime import datetime, timezone
from pathlib import Path

from blackbox.client import BlackBoxClient
from blackbox.rules import MUTATION_ORDER
from simulate.run import RunConfig, run
from simulate.scenarios import TEMPLATES

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_ROOT.parents[1]
DEFAULT_OUT = REPO_ROOT / "dig-site"
SEALED_RUNS = SERVICE_ROOT / "sealed" / "runs"
MANUAL = SERVICE_ROOT / "tools" / "assets" / "pressure-manual-v1.md"

BINARY_MODULES = ("__init__", "api", "cards", "engine", "rules", "scripts", "server", "state", "view")
REMOVED_FUNCTIONS = ("_gear", "_vent", "_reverse", "_double", "_jam", "_draw")
GENERATED = ("bin", "traces", "source", "docs", "MANIFEST.json")
SCENARIO_IDS = {name: f"s{index + 1:02d}" for index, name in enumerate(TEMPLATES)}
CHECK_GAMES = 20

# Names that must never be readable in the binary's raw bytes.
SECRET_STRINGS = tuple(m.value for m in MUTATION_ORDER) + (
    "vent_amount", "overflow_reset", "scan_depth", "stacking_double", "jam_blocks_draw", "reverse_skips",
)


class BuildError(RuntimeError):
    """A dig-site artifact failed its check."""


# --- documentation ---------------------------------------------------------


def build_docs(out: Path) -> Path:
    target = out / "docs" / MANUAL.name
    target.parent.mkdir(parents=True)
    shutil.copyfile(MANUAL, target)
    return target


# --- partial code ----------------------------------------------------------


_SOURCE_HEADER = (
    "# Recovered source fragment of the Pressure engine.\n"
    "# Some function bodies were lost and now raise NotImplementedError.\n"
    "# Several modules this file imports were not recovered.\n\n"
)


def partial_source(source: str, removed: tuple[str, ...]) -> str:
    """Return ``source`` with the bodies of ``removed`` functions lost. Comments are dropped."""
    tree = ast.parse(source)
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in removed:
            lost = ast.Call(ast.Name("NotImplementedError", ast.Load()), [ast.Constant("missing from recovered source")], [])
            node.body = [ast.Raise(exc=lost)]
            found.add(node.name)
    missing = set(removed) - found
    if missing:
        raise BuildError(f"functions to remove not found: {sorted(missing)}")
    return _SOURCE_HEADER + ast.unparse(tree) + "\n"


def build_source(out: Path) -> list[Path]:
    folder = out / "source"
    folder.mkdir(parents=True)
    engine = folder / "engine.py"
    engine.write_text(partial_source((SERVICE_ROOT / "blackbox" / "engine.py").read_text(), REMOVED_FUNCTIONS))
    state = folder / "state.py"
    state.write_text(_SOURCE_HEADER.splitlines()[0] + "\n\n" + ast.unparse(ast.parse((SERVICE_ROOT / "blackbox" / "state.py").read_text())) + "\n")
    return [engine, state]


# --- binary ----------------------------------------------------------------


def build_binary(out: Path, seed: int, mutations: int) -> Path:
    """Bytecode-only, docstring-free zipapp with the run's settings baked in."""
    target = out / "bin" / "pressure.pyz"
    target.parent.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "blackbox"
        package.mkdir()
        for module in BINARY_MODULES:
            py_compile.compile(
                str(SERVICE_ROOT / "blackbox" / f"{module}.py"),
                cfile=str(package / f"{module}.pyc"),
                doraise=True,
                optimize=2,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
        entry = Path(tmp) / "_entry.py"
        argv = ["--seed", str(seed), "--mutations", str(mutations)]
        entry.write_text(f"import sys\nfrom blackbox.server import main\n\ndef run():\n    sys.exit(main({argv!r}))\n")
        py_compile.compile(str(entry), cfile=str(package / "_entry.pyc"), doraise=True, optimize=2,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        entry.unlink()
        (Path(tmp) / "__main__.py").write_text("from blackbox._entry import run\nrun()\n")
        zipapp.create_archive(tmp, target, interpreter="/usr/bin/env python3", compressed=True)
    target.chmod(0o755)
    return target


def check_binary_strings(binary: Path) -> None:
    raw = binary.read_bytes()
    leaked = [name for name in SECRET_STRINGS if name.encode() in raw]
    if leaked:
        raise BuildError(f"binary leaks readable names: {leaked}")


def check_binary_matches_service(binary: Path, seed: int, mutations: int, games: int = CHECK_GAMES) -> int:
    """Play the same games on the binary and the service and require identical replies. Returns steps checked."""
    steps = 0
    with BlackBoxClient(seed=seed, mutations=mutations) as service, \
            BlackBoxClient(command=[sys.executable, str(binary)]) as compiled:
        for index in range(games):
            script = service.random_script(seed * 7_919 + index)
            if compiled.random_script(seed * 7_919 + index) != script:
                raise BuildError("binary and service disagree on random_script")
            players = 2 + index % 3
            game, other = service.new_game(players, script), compiled.new_game(players, script)
            if game != other:
                raise BuildError(f"binary and service disagree on new_game {index}")
            rng = random.Random(index)
            game_id = game["game_id"]
            while True:
                legal = service.legal_actions(game_id)
                if compiled.legal_actions(game_id) != legal:
                    raise BuildError(f"binary and service disagree on legal actions in game {index}")
                if not legal:
                    break
                action = rng.choice(legal)
                result = service.act(game_id, action)
                if compiled.act(game_id, action) != result:
                    raise BuildError(f"binary and service disagree on act in game {index}, step {steps}")
                steps += 1
    return steps


# --- runtime traces --------------------------------------------------------


def _neutral_label(label: str) -> str:
    kind, _, name = label.partition(":")
    return f"scenario:{SCENARIO_IDS[name]}" if kind == "scenario" else label


def _rewrite_jsonl(source: Path, target: Path) -> int:
    count = 0
    with source.open(encoding="utf-8") as reader, target.open("w", encoding="utf-8") as writer:
        for line in reader:
            record = json.loads(line)
            record["label"] = _neutral_label(record["label"])
            writer.write(json.dumps(record, separators=(",", ":")) + "\n")
            count += 1
    return count


def build_traces(out: Path, seed: int, mutations: int, games: int, run_id: str) -> tuple[Path, dict]:
    """Record a seed corpus from the same build and seal the planted mutations."""
    target = out / "traces" / "seed-corpus"
    target.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        sealed_tmp = Path(tmp) / "sealed.json"
        run_dir = run(RunConfig(games=games, seed=seed, mutations=mutations, run_id=run_id,
                                out=Path(tmp) / "runs", sealed_out=sealed_tmp))
        traces = _rewrite_jsonl(run_dir / "traces.jsonl", target / "traces.jsonl")
        played = _rewrite_jsonl(run_dir / "games.jsonl", target / "games.jsonl")
        source_manifest = json.loads((run_dir / "manifest.json").read_text())
        planted = json.loads(sealed_tmp.read_text())["mutations"]
    manifest = {"run_id": run_id, "games": played, "traces": traces, "heldout_games": source_manifest["heldout_games"],
                "status": source_manifest["status"]}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    sealed = {"run_id": run_id, "seed": seed, "mutation_count": mutations, "mutations": planted}
    return target, sealed


# --- manifest --------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(out: Path, path: Path, kind: str, tier: str, description: str) -> dict:
    files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
    return {
        "path": str(path.relative_to(out)),
        "kind": kind,
        "trust_tier": tier,
        "description": description,
        "files": [{"path": str(f.relative_to(out)), "bytes": f.stat().st_size, "sha256": _sha256(f)} for f in files],
    }


def build_manifest(out: Path, binary: Path, traces: Path, docs: Path, run_id: str, corpus: dict) -> dict:
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    manifest = {
        "dig_site": "pressure",
        "format": 1,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "artifacts": [
            _entry(out, binary, "binary", "observed",
                   f"The running program. Speaks JSON lines on stdin/stdout. Run: python{python} bin/pressure.pyz. "
                   f"Needs Python {python} and pydantic>=2.7. Ignores command-line flags."),
            _entry(out, traces, "runtime_traces", "observed",
                   f"Seed corpus recorded from this binary: {corpus['games']} games, {corpus['traces']} steps. "
                   "Correct but incomplete coverage."),
            _entry(out, out / "source", "partial_code", "read_from_code",
                   "Recovered fragment of the engine. Some function bodies are lost; several imported modules are missing."),
            _entry(out, docs, "documentation", "documented",
                   "Player's manual, version 1.0. A later revision of section 4 is missing."),
        ],
        "trust_rules": [
            "Behavior wins: only traces from running the binary can confirm or refute a claim.",
            "A claim taken from documentation or code keeps its tier until traces back it up.",
            "When an artifact and the traces disagree, the artifact is wrong and the claim is refuted with the trace ID.",
        ],
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# --- build -----------------------------------------------------------------


def build(out: Path, seed: int, mutations: int, games: int, run_id: str, sealed_dir: Path = SEALED_RUNS) -> dict:
    if not 0 <= mutations <= len(MUTATION_ORDER):
        raise ValueError(f"mutations must be 0-{len(MUTATION_ORDER)}")
    if not 1 <= games <= 100_000:
        raise ValueError("games must be 1-100000")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", run_id):
        raise ValueError("run_id may only contain letters, digits and dashes")
    out.mkdir(parents=True, exist_ok=True)
    for name in GENERATED:  # only remove what this tool generates; README.md and anything else stay
        path = out / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    docs = build_docs(out)
    build_source(out)
    binary = build_binary(out, seed, mutations)
    check_binary_strings(binary)
    checked_steps = check_binary_matches_service(binary, seed, mutations)
    traces, sealed = build_traces(out, seed, mutations, games, run_id)
    corpus = json.loads((traces / "manifest.json").read_text())
    manifest = build_manifest(out, binary, traces, docs, run_id, corpus)

    sealed_dir.mkdir(parents=True, exist_ok=True)
    (sealed_dir / f"{run_id}.json").write_text(json.dumps(sealed, indent=2) + "\n", encoding="utf-8")
    return {"out": out, "manifest": manifest, "checked_steps": checked_steps, "corpus": corpus,
            "sealed": sealed_dir / f"{run_id}.json"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the dig-site evidence for Strata")
    parser.add_argument("--seed", type=int, default=42, help="run seed; picks the hidden mutations")
    parser.add_argument("--mutations", type=int, default=2, help="how many mutations to plant (0-6)")
    parser.add_argument("--games", type=int, default=40, help="games in the seed corpus")
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    result = build(args.out, args.seed, args.mutations, args.games, args.run_id)
    corpus = result["corpus"]
    print(f"dig-site built at {result['out']}")
    print(f"  binary matches the service on {result['checked_steps']} steps across {CHECK_GAMES} games")
    print(f"  seed corpus: {corpus['games']} games, {corpus['traces']} traces")
    print(f"  planted mutations sealed in {result['sealed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
