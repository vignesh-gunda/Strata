"""T16.1, T16.7, T16.8: the UI's door into the agent.

The page sends one line. The bridge hands it to the agent's coordinator (which uses the
concierge for free-form lines) and returns the plain-language reply right away. Long work
(a rebuild) runs on a background thread inside the coordinator, so the reply never waits for it.

What the bridge adds on top of the coordinator:
  * input limits and a rate limit, so the page cannot flood the models
  * one run at a time (the coordinator already refuses a second start; the bridge reports it)
  * a Cancel that stops the running stage cleanly and leaves the ledger and traces intact
  * a status read for the page: where the run is, which models are on, tokens used

Model keys are read from .env by the agent's clients. They are never sent to the page.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

MAX_ASK_CHARS = 400
ASKS_PER_WINDOW = 12
WINDOW_SECONDS = 60.0
CANCELLED_CODE = 130
MAX_MESSAGES = 30
# How hard Muse thinks. Lower answers faster; Muse Spark cannot switch thinking off, so "minimal"
# is the fastest. Measured on one short question: unset 5.9 s, "low" 4.3 s, "minimal" 2.2 s.
ASK_EFFORT = "minimal"        # the ask box: short plain answers
STAGE_EFFORT = "low"          # the pipeline stages: rules and code, so a little more thought
LOCK_NAME = "stage.lock"      # one stage process per run folder, across servers


def _agent_python(agent_root: Path) -> str:
    """The agent's own interpreter when it has one, so stages get the same packages."""
    venv = agent_root / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StageBusy(RuntimeError):
    """Another process is already running a stage for this run folder."""


def take_lock(run_dir: Path) -> Path:
    """Claim the run folder for one stage. A lock left by a dead process is taken over."""
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = run_dir / LOCK_NAME
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                raise StageBusy(f"another process (pid {pid}) is already working on this run") from None
            lock.unlink(missing_ok=True)       # stale: its owner is gone
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return lock
    raise StageBusy("could not claim the run folder")


class CancellableStageRunner:
    """Runs one pipeline stage as `python -m <module>`, like the coordinator's default runner,
    but keeps a handle on the child so Cancel can stop it, gives it a faster Muse setting,
    and holds a lock so two servers can never run stages on the same run at once."""

    def __init__(self, agent_root: Path, *, effort: str | None = None):
        self._root = Path(agent_root)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self.effort = effort or os.environ.get("STRATA_STAGE_EFFORT") or STAGE_EFFORT
        self.current: str | None = None
        self.started_at: float | None = None

    def __call__(self, module: str, run_id: str, runs_dir: Path, *extra: str):
        from coordinator.main.runs import StageResult

        with self._lock:
            if self._cancelled:
                return StageResult(CANCELLED_CODE, "cancelled by you")
            try:
                lock = take_lock(Path(runs_dir) / run_id)
            except StageBusy as busy:
                return StageResult(CANCELLED_CODE, str(busy))
            env = {**os.environ, "STRATA_MUSE_EFFORT": self.effort}
            proc = subprocess.Popen(
                [_agent_python(self._root), "-m", module, "--run-id", run_id, "--runs-dir", str(runs_dir), *extra],
                cwd=self._root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                start_new_session=True)       # its own process group, so Cancel reaches grandchildren
            self._proc = proc
            self.current, self.started_at = module, time.time()
        try:
            output, _ = proc.communicate()
        finally:
            lock.unlink(missing_ok=True)
        with self._lock:
            self._proc = None
            self.current, self.started_at = None, None
            cancelled = self._cancelled
        if cancelled:
            return StageResult(CANCELLED_CODE, "cancelled by you")
        lines = [line for line in (output or "").strip().splitlines() if line.strip()]
        return StageResult(proc.returncode, lines[-1][:200] if lines else "")

    def cancel(self) -> bool:
        """Stop the running stage. Returns True if there was one to stop."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        return True

    def reset(self) -> None:
        with self._lock:
            self._cancelled = False


def _default_muse() -> Any:
    """The ask box wants quick plain answers, so Muse thinks as little as it can. Set
    STRATA_ASK_EFFORT to change it."""
    from llm.main.muse import MuseClient
    return MuseClient(effort=os.environ.get("STRATA_ASK_EFFORT") or ASK_EFFORT)


def _default_subq() -> Any:
    from llm.main.subq import SubqClient
    return SubqClient()


class AgentBridge:
    def __init__(self, runs_dir: Path, run_id: str, *,
                 muse_factory: Callable[[], Any] = _default_muse,
                 subq_factory: Callable[[], Any] = _default_subq,
                 stage_runner: Callable[..., Any] | None = None,
                 agent_root: Path | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] | None = None,
                 which: Callable[[str], str | None] | None = None):
        self.runs_dir = Path(runs_dir)
        self.run_id = run_id
        self.run_dir = self.runs_dir / run_id
        self._agent_root = agent_root or Path(__file__).resolve().parents[1] / "agent"
        self._muse_factory, self._subq_factory = muse_factory, subq_factory
        self._clock = clock
        self._sleep = sleep
        self._which = which
        self._cancel_runner = None if stage_runner else CancellableStageRunner(self._agent_root)
        self._stage_runner = stage_runner or self._cancel_runner
        self._lock = threading.Lock()          # one question at a time, in order
        self._asks: deque[float] = deque()
        self._messages: deque[str] = deque(maxlen=MAX_MESSAGES)
        self._coordinator: Any = None
        self._muse: Any = None
        self._subq: Any = None
        self._notes: list[str] = []

    # ---------- setup ----------
    def _emit(self, message: str) -> None:
        self._messages.append(message)

    def _models(self) -> None:
        """Create the model clients once. A missing key just turns that model off."""
        for name, factory in (("muse", self._muse_factory), ("subq", self._subq_factory)):
            try:
                setattr(self, f"_{name}", factory())
            except Exception as exc:        # missing key, bad config: say so plainly, keep going
                setattr(self, f"_{name}", None)
                self._notes.append(f"{name} is off ({type(exc).__name__})")

    def _coordinator_ready(self) -> Any:
        if self._coordinator is None:
            from coordinator.main.chat import Coordinator
            self._models()
            self._coordinator = Coordinator(self.runs_dir, self.run_id, self._emit,
                                            subq=self._subq, muse=self._muse,
                                            stage_runner=self._stage_runner, sleep=self._sleep,
                                            which=self._which)
        return self._coordinator

    def _allow(self) -> bool:
        now = self._clock()
        while self._asks and now - self._asks[0] > WINDOW_SECONDS:
            self._asks.popleft()
        if len(self._asks) >= ASKS_PER_WINDOW:
            return False
        self._asks.append(now)
        return True

    # ---------- actions ----------
    def ask(self, text: Any) -> dict:
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "reply": "Type a question or a request first.", "running": self.running()}
        text = text.strip()
        if len(text) > MAX_ASK_CHARS:
            return {"ok": False, "reply": f"Please keep it under {MAX_ASK_CHARS} characters.",
                    "running": self.running()}
        with self._lock:
            if not self._allow():
                return {"ok": False, "reply": "That is a lot of questions at once. Give it a minute and try again.",
                        "running": self.running()}
            coordinator = self._coordinator_ready()
            if self._cancel_runner is not None and not coordinator.runner.running():
                self._cancel_runner.reset()       # a fresh request after a Cancel starts clean
            try:
                reply = coordinator.handle(text)
            except Exception as exc:               # never show a stack trace to the page
                return {"ok": False, "reply": f"Something went wrong while answering ({type(exc).__name__}). "
                                              "Your progress is saved.", "running": self.running()}
        return {"ok": True, "reply": reply, "running": self.running()}

    def cancel(self) -> dict:
        if not self.running():
            return {"ok": True, "cancelled": False, "reply": "Nothing is running."}
        self._coordinator.runner.request_stop(remove_code=True)   # the code it was writing goes too
        stopped = self._cancel_runner.cancel() if self._cancel_runner else False
        return {"ok": True, "cancelled": True,
                "reply": ("Stopping the run and removing the code it had started writing. What it learned "
                          "is saved; say \"start\" to resume." if stopped
                          else "Cancel requested. It will stop at the next step and remove its code.")}

    def running(self) -> bool:
        return bool(self._coordinator is not None and self._coordinator.runner.running())

    def shutdown(self) -> None:
        """The server is stopping: stop the stage too, so no orphan keeps writing to the run."""
        if self._cancel_runner is not None:
            self._cancel_runner.cancel()

    def _progress(self) -> dict:
        """When the agent last posted a line, and what it is busy with right now."""
        events = self.run_dir / "events.jsonl"
        try:
            last = events.stat().st_mtime
        except OSError:
            last = None
        runner = self._cancel_runner
        stage = runner.current.split(".")[0] if runner and runner.current else None
        return {"last_line_at": last, "now": time.time(), "stage": stage,
                "stage_started_at": runner.started_at if runner else None,
                "stage_effort": runner.effort if runner else None}

    def status(self) -> dict:
        coordinator = self._coordinator
        if coordinator is None:
            status_line = f"Run {self.run_id} is ready. Ask for something to begin."
        else:
            status_line = coordinator.runner.status_line()
        budget: dict[str, dict] = {}
        for name, client in (("muse", self._muse), ("subq", self._subq)):
            if client is not None:
                budget[name] = client.budget.summary().get(name, {})
        return {
            "enabled": True,
            "run_id": self.run_id,
            "running": self.running(),
            "status": status_line,
            "models": {"muse": self._muse is not None, "subq": self._subq is not None,
                       "started": self._coordinator is not None},
            "budget": budget,
            "budget_note": "Counts the model calls made by the ask box. Pipeline stages keep their own totals.",
            "notes": self._notes[-3:],
            "progress": self._progress(),
            "messages": list(self._messages)[-10:],
        }
