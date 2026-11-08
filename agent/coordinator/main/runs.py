"""T10.3: run the pipeline in the background and report progress as it goes.

Each stage is a separate process (`python -m <module> --run-id ...`), so a crash in
one stage cannot take the chat down. Stages are idempotent, and `coordinator_state.json`
records which ones finished, so a restart resumes at the first unfinished stage.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from llm.main import firewall

from .reliability import RunState, load_ledger, with_retry

AGENT_ROOT = Path(__file__).resolve().parents[2]

# (stage name, module, what the user sees)
STAGES: list[tuple[str, str, str]] = [
    ("survey", "surveyor.main.survey", "Reading the manual and partial code"),
    ("audit", "auditor.main.audit", "Checking those claims against the recorded games"),
    ("hypothesize", "hypothesizer.main.hypothesize", "Playing test games and testing rules"),
    ("rebuild", "rebuilder.main.cli", "Rebuilding the game and checking it against the original"),
]
# Exit codes that are a real outcome, not a crash.
_OK_CODES = {"rebuild": {0, 1, 2}}


@dataclass
class StageResult:
    code: int
    tail: str = ""


StageRunner = Callable[[str, str, Path], StageResult]     # (module, run_id, runs_dir)
Emit = Callable[[str], None]


def subprocess_stage(module: str, run_id: str, runs_dir: Path) -> StageResult:
    proc = subprocess.run(
        [sys.executable, "-m", module, "--run-id", run_id, "--runs-dir", str(runs_dir)],
        cwd=AGENT_ROOT, capture_output=True, text=True)
    lines = [line for line in (proc.stdout + proc.stderr).strip().splitlines() if line.strip()]
    return StageResult(proc.returncode, lines[-1][:200] if lines else "")


def _safe(text: str) -> str:
    try:
        firewall.check(text)
        return text
    except firewall.PromptFirewallError:
        return "[output hidden]"


class RunManager:
    def __init__(self, runs_dir: Path, run_id: str, emit: Emit, *,
                 stage_runner: StageRunner = subprocess_stage,
                 stage_attempts: int = 2, sleep: Callable[[float], None] | None = None):
        self.runs_dir = Path(runs_dir)
        self.run_id = run_id
        self.run_dir = self.runs_dir / run_id
        self._emit = emit
        self._stage_runner = stage_runner
        self._attempts = stage_attempts
        self._sleep = sleep
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---------- public ----------
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> str:
        """Return the immediate acknowledgement; the work happens on a background thread."""
        with self._lock:
            if self.running():
                return f"A run is already going ({self.state().current or 'starting'}). Ask for status any time."
            state = RunState.load(self.run_dir, self.run_id)
            if state.finished:
                return ("This run already finished. Ask for the report or the rebuild, "
                        "or start the agent with a new --run-id for a fresh run.")
            done = len(state.completed)
            resume = f" Resuming after {', '.join(state.completed)}." if done else ""
            self._thread = threading.Thread(target=self._work, name=f"run-{self.run_id}", daemon=True)
            self._thread.start()
            return f"Started run {self.run_id}.{resume} I will post progress here."

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def state(self) -> RunState:
        return RunState.load(self.run_dir, self.run_id)

    def status_line(self) -> str:
        state = self.state()
        total = len(STAGES)
        if state.finished:
            return f"Run {self.run_id} is finished ({total}/{total} stages)."
        if state.failed:
            return (f"Run {self.run_id} stopped at {state.failed} after {len(state.completed)}/{total} stages: "
                    f"{state.notes.get(state.failed, 'no detail')}. Say \"start\" to resume.")
        if self.running():
            label = next((d for n, _, d in STAGES if n == state.current), "Working")
            return f"Run {self.run_id} is going: {label} (stage {len(state.completed) + 1}/{total})."
        if state.completed:
            return (f"Run {self.run_id} is paused after {len(state.completed)}/{total} stages "
                    f"({', '.join(state.completed)}). Say \"start\" to resume.")
        return f"Run {self.run_id} has not started. Say \"start\"."

    # ---------- worker ----------
    def _work(self) -> None:
        state = RunState.load(self.run_dir, self.run_id)
        state.failed = None
        try:
            for name, module, label in STAGES:
                if name in state.completed:
                    continue
                state.current = name
                state.save(self.run_dir)
                self._emit(f"[{len(state.completed) + 1}/{len(STAGES)}] {label}...")
                result = self._run_stage(name, module)
                if result.code not in _OK_CODES.get(name, {0}):
                    state.failed, state.current = name, None
                    state.notes[name] = _safe(result.tail or f"exit code {result.code}")
                    state.save(self.run_dir)
                    self._emit(f"Stopped at {name}: {state.notes[name]}. "
                               "Your progress is saved; say \"start\" to resume.")
                    return
                state.completed.append(name)
                state.current = None
                state.notes[name] = _safe(result.tail)
                state.save(self.run_dir)
                self._emit(self._after_stage(name, result))
            state.finished = True
            state.save(self.run_dir)
            self._emit(f"Run {self.run_id} finished. Ask for the report or the rebuild.")
        except Exception as exc:  # never let the thread die silently
            state.failed, state.current = state.current or "unknown", None
            state.notes[state.failed] = f"{type(exc).__name__}"
            state.save(self.run_dir)
            self._emit(f"Stopped unexpectedly ({type(exc).__name__}). Progress is saved; say \"start\" to resume.")

    def _run_stage(self, name: str, module: str) -> StageResult:
        ok = _OK_CODES.get(name, {0})

        def attempt() -> StageResult:
            result = self._stage_runner(module, self.run_id, self.runs_dir)
            if result.code not in ok:
                raise _StageFailed(result)
            return result

        try:
            return with_retry(attempt, attempts=self._attempts, base_delay=2.0, sleep=self._sleep,
                              retry_on=lambda e: isinstance(e, _StageFailed))
        except _StageFailed as failed:
            return failed.result

    def _after_stage(self, name: str, result: StageResult) -> str:
        """One factual progress line, taken from the ledger and not from stage output."""
        view = load_ledger(self.run_dir)
        counts = view.ledger.counts() if view.ledger else {}
        total = sum(counts.values())
        if name == "rebuild":
            if result.code == 2:
                return "No proven rules yet, so nothing to rebuild. Ask for the open questions."
            return "Rebuild " + ("accepted by the checker." if result.code == 0 else "was blocked by the checker; see the rebuild summary.")
        if not total:
            return f"Finished {name}."
        return (f"Finished {name}: {total} claims, {counts.get('confirmed', 0)} proven, "
                f"{counts.get('refuted', 0)} wrong, {counts.get('open', 0)} unknown.")


class _StageFailed(Exception):
    def __init__(self, result: StageResult):
        super().__init__(result.tail)
        self.result = result
