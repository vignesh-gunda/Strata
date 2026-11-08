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

from livefeed.main.stream import FeedWatcher
from llm.main import firewall

from rebuilder.main import scaffold
from rebuilder.main.languages import DEFAULT_LANGUAGE, LANGUAGES, LanguageConfig

from .reliability import RunState, load_ledger, with_retry

AGENT_ROOT = Path(__file__).resolve().parents[2]

# (stage name, module, what the user sees)
STAGES: list[tuple[str, str, str]] = [
    ("survey", "surveyor.main.survey", "Reading the manual and partial code"),
    ("audit", "auditor.main.audit", "Checking those claims against the recorded games"),
    ("hypothesize", "hypothesizer.main.hypothesize", "Playing test games and testing rules"),
    ("rebuild", "rebuilder.main.cli", "Rebuilding the game and checking it against the original"),
    ("report", "report.main.cli", "Writing the report"),
]
# Exit codes that are a real outcome, not a crash.
_OK_CODES = {"rebuild": {0, 1, 2}}
LANGUAGE_STAGES = {"rebuild"}        # stages that take --language
# "quick" (the default): short traces, so the agent gets to rules, then the rebuild folder, sooner.
# "full": the original probe counts and game lengths.
PROFILE_ARGS: dict[str, dict[str, tuple[str, ...]]] = {"quick": {"hypothesize": ("--quick",)}, "full": {}}
DEFAULT_PROFILE = "quick"
MAX_RELEARN_CYCLES = 1               # T16.10: extra hypothesize -> rebuild rounds after a blocked rebuild
DIVERGENCE_NOTE = "the rebuild diverged from the original"


@dataclass
class StageResult:
    code: int
    tail: str = ""


StageRunner = Callable[..., StageResult]     # (module, run_id, runs_dir, *extra_args)
Emit = Callable[[str], None]


def subprocess_stage(module: str, run_id: str, runs_dir: Path, *extra: str) -> StageResult:
    proc = subprocess.run(
        [sys.executable, "-m", module, "--run-id", run_id, "--runs-dir", str(runs_dir), *extra],
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
                 stage_attempts: int = 2, sleep: Callable[[float], None] | None = None,
                 profile: str = DEFAULT_PROFILE):
        if profile not in PROFILE_ARGS:
            raise ValueError(f"unknown profile {profile!r}")
        self.profile = profile
        self.runs_dir = Path(runs_dir)
        self.run_id = run_id
        self.run_dir = self.runs_dir / run_id
        self._emit = emit
        self._stage_runner = stage_runner
        self._attempts = stage_attempts
        self._sleep = sleep
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._discard_code = False          # set by request_stop(): delete the rebuild when the run ends

    # ---------- public ----------
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, language: LanguageConfig | None = None) -> str:
        """Return the immediate acknowledgement; the work happens on a background thread.

        `language` is where the rebuild stage writes its code. Asking for a different language
        after a finished run reuses the survey, probes and proven rules and only rebuilds.
        """
        with self._lock:
            if self.running():
                return f"A run is already going ({self.state().current or 'starting'}). Ask for status any time."
            state = RunState.load(self.run_dir, self.run_id)
            wanted = language.key if language else (state.language or DEFAULT_LANGUAGE)
            if state.finished and wanted != state.language:
                state.completed = [s for s in state.completed if s != "rebuild"]
                state.finished = False
                state.failed = None
            elif state.finished:
                return ("This run already finished. Ask for the report or the rebuild, "
                        "or start the agent with a new --run-id for a fresh run.")
            state.language = wanted
            state.save(self.run_dir)
            self._discard_code = False
            try:                          # the folder appears the moment the rebuild is asked for
                scaffold.refresh(self.run_dir, LANGUAGES[wanted])
            except OSError:
                pass
            done = len(state.completed)
            resume = f" Resuming after {', '.join(state.completed)}." if done else ""
            self._thread = threading.Thread(target=self._work, name=f"run-{self.run_id}", daemon=True)
            self._thread.start()
            return (f"Started run {self.run_id}: rebuilding in {LANGUAGES[wanted].name}.{resume} "
                    "I will post progress here.")

    def request_stop(self, *, remove_code: bool = True) -> None:
        """The user stopped the run. When it has wound down, its rebuild folder is deleted.

        The deletion happens after the run's thread is done, so nothing can write the files
        back. If nothing is running, the folder is deleted right away."""
        with self._lock:
            self._discard_code = remove_code
            running = self.running()
        if not running and remove_code:
            self._remove_code()

    def _remove_code(self) -> None:
        state = RunState.load(self.run_dir, self.run_id)
        try:
            removed = scaffold.remove(self.run_dir, LANGUAGES[state.language or DEFAULT_LANGUAGE])
        except OSError:
            removed = False
        with self._lock:
            self._discard_code = False
        if removed:
            self._emit("Run stopped: removed the code it had started writing.")

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
        # T15.3: each loop the agent finishes is posted here as it happens
        feed = FeedWatcher(self.run_dir, self._emit).start()
        files = FolderWriter(self.run_dir, LANGUAGES[state.language or DEFAULT_LANGUAGE]).start()
        try:
            cycles = 0
            while True:
                queued_again = False
                for name, module, label in STAGES:
                    if name in state.completed:
                        continue
                    state.current = name
                    state.save(self.run_dir)
                    self._emit(f"[{len(state.completed) + 1}/{len(STAGES)}] {label}...")
                    result = self._run_stage(name, module, state.language)
                    feed.drain()                 # loop lines come before the stage summary
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
                    if (name == "rebuild" and result.code == 1 and cycles < MAX_RELEARN_CYCLES
                            and self._relearn(state, cycles + 1)):
                        cycles += 1              # T16.10: learn from the difference, then rebuild again
                        queued_again = True
                        break
                if not queued_again:
                    break
            state.finished = True
            state.save(self.run_dir)
            self._emit(f"Run {self.run_id} finished. Ask for the report or the rebuild.")
        except Exception as exc:  # never let the thread die silently
            state.failed, state.current = state.current or "unknown", None
            state.notes[state.failed] = f"{type(exc).__name__}"
            state.save(self.run_dir)
            self._emit(f"Stopped unexpectedly ({type(exc).__name__}). Progress is saved; say \"start\" to resume.")
        finally:
            feed.stop()
            files.stop(final=not self._discard_code)
            if self._discard_code:
                self._remove_code()

    def _relearn(self, state: RunState, round_number: int) -> bool:
        """T16.10: a blocked rebuild that demoted rules gets one more round of testing those rules.

        The Verifier's difference was stored as a trace and the rules it touched were demoted to
        Inferred. Running hypothesize and rebuild again lets the agent prove them against that
        trace and rebuild from what held up. Returns True when another round was queued.
        """
        view = load_ledger(self.run_dir, snapshot=False)
        if view.ledger is None:
            return False
        touched = [c for c in view.ledger.all_claims()
                   if c.status.value == "inferred" and any(DIVERGENCE_NOTE in n for n in c.notes)]
        if not touched:
            return False
        state.completed = [s for s in state.completed if s not in ("hypothesize", "rebuild", "report")]
        state.save(self.run_dir)
        self._emit(f"The rebuild disagreed with the original on {len(touched)} proven rule(s), so I am testing "
                   f"those rules again with the new evidence (round {round_number + 1}).")
        return True

    def _run_stage(self, name: str, module: str, language: str = DEFAULT_LANGUAGE) -> StageResult:
        ok = _OK_CODES.get(name, {0})
        extra = (("--language", language) if name in LANGUAGE_STAGES else ()) \
            + PROFILE_ARGS[self.profile].get(name, ())

        def attempt() -> StageResult:
            result = self._stage_runner(module, self.run_id, self.runs_dir, *extra)
            if result.code not in ok:
                raise _StageFailed(result)
            return result

        try:
            return with_retry(attempt, attempts=self._attempts, base_delay=1.0, sleep=self._sleep,
                              retry_on=lambda e: isinstance(e, _StageFailed))
        except _StageFailed as failed:
            return failed.result

    def _after_stage(self, name: str, result: StageResult) -> str:
        """One factual progress line, taken from the ledger and not from stage output."""
        view = load_ledger(self.run_dir)
        counts = view.ledger.counts() if view.ledger else {}
        total = sum(counts.values())
        if name == "report":
            return f"The report is written: {self.run_dir / 'report.md'}"
        if name == "rebuild":
            if result.code == 2:
                return "No proven rules yet, so nothing to rebuild. Ask for the open questions."
            return "Rebuild " + ("accepted by the checker." if result.code == 0 else "was blocked by the checker; see the rebuild summary.")
        if not total:
            return f"Finished {name}."
        return (f"Finished {name}: {total} claims, {counts.get('confirmed', 0)} proven, "
                f"{counts.get('refuted', 0)} wrong, {counts.get('open', 0)} unknown.")


class FolderWriter:
    """Keeps the rebuild folder's notes and rule files in step with the ledger while a run works.

    Polls the ledger file and rewrites only when it changed, so the UI file panel shows the
    folder growing as the agent learns. A failure here never stops the run.
    """

    def __init__(self, run_dir: Path, language: LanguageConfig, poll: float = 1.5):
        self.run_dir, self.language, self.poll = Path(run_dir), language, poll
        self._stop = threading.Event()
        self._seen: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None

    def _ledger_stamp(self) -> tuple[int, int] | None:
        try:
            info = (self.run_dir / "ledger.jsonl").stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def tick(self) -> list[str]:
        stamp = self._ledger_stamp()
        if stamp is None or stamp == self._seen:
            return []
        self._seen = stamp
        try:
            return scaffold.refresh(self.run_dir, self.language)
        except Exception:
            return []

    def start(self) -> "FolderWriter":
        self._thread = threading.Thread(target=self._loop, name="folder-writer", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        self.tick()
        while not self._stop.wait(self.poll):
            self.tick()

    def stop(self, final: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        if final:
            self.tick()


class _StageFailed(Exception):
    def __init__(self, result: StageResult):
        super().__init__(result.tail)
        self.result = result
