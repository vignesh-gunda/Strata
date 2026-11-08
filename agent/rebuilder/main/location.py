"""Where a run's rebuilt code goes: `<repo>/rebuild/<run_id>/<language>/`.

The rebuild is the product, so it lives at the top of the project, next to `agent/` and
`ui/`, not buried in the agent's run folder. Each run keeps its own sub-folder so two runs
(or a rerun in another language) never overwrite each other.

Only runs in the agent's own `agent/runs/` folder are placed there. A run kept anywhere else
(a test's temporary folder, a rehearsal) keeps its rebuild inside that run folder, so tests
and rehearsals never write into the project. `STRATA_REBUILD_ROOT` overrides the location.
"""
from __future__ import annotations

import os
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = AGENT_ROOT.parent
DEFAULT_RUNS = AGENT_ROOT / "runs"
DEFAULT_REBUILD_ROOT = REPO_ROOT / "rebuild"


def rebuild_dir(run_dir: str | Path) -> Path:
    """The folder that holds every language rebuilt for this run (one sub-folder per language)."""
    run_dir = Path(run_dir)
    override = os.environ.get("STRATA_REBUILD_ROOT")
    if override:
        return Path(override) / run_dir.name
    if run_dir.resolve().parent == DEFAULT_RUNS.resolve():
        return DEFAULT_REBUILD_ROOT / run_dir.name
    return run_dir / "rebuild"
