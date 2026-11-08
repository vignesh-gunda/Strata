"""T10.5: retries, last-good-ledger fallback, and resumable run state."""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from ledger.main.store import Ledger

T = TypeVar("T")


def with_retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 0.5,
               sleep: Callable[[float], None] | None = None,
               retry_on: Callable[[BaseException], bool] = lambda exc: True) -> T:
    """Call `fn`, retrying with exponential backoff while `retry_on(exc)` is true."""
    sleep = sleep or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts or not retry_on(exc):
                raise
            sleep(base_delay * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


@dataclass
class LedgerView:
    ledger: Ledger | None
    degraded: bool = False      # True when we fell back to the last good copy
    note: str = ""


def _good_copy(run_dir: Path) -> Path:
    return run_dir / "ledger.good.jsonl"


def load_ledger(run_dir: Path) -> LedgerView:
    """Open the run's ledger. If it will not load, use the last good snapshot.

    A successful load refreshes the snapshot, so the fallback is never older than the
    last time the ledger was readable.
    """
    live = run_dir / "ledger.jsonl"
    good = _good_copy(run_dir)
    if live.exists():
        try:
            ledger = Ledger(live)
        except Exception as exc:
            if good.exists():
                try:
                    return LedgerView(Ledger(good), True,
                                      f"live ledger unreadable ({type(exc).__name__}); showing the last good copy")
                except Exception:
                    pass
            return LedgerView(None, True, f"ledger unreadable ({type(exc).__name__}) and no good copy")
        tmp = good.with_suffix(".tmp")
        shutil.copyfile(live, tmp)
        os.replace(tmp, good)
        return LedgerView(ledger)
    if good.exists():
        return LedgerView(Ledger(good), True, "live ledger missing; showing the last good copy")
    return LedgerView(None, False, "no ledger yet")


@dataclass
class RunState:
    """Saved after every stage so a restart resumes instead of starting over."""
    run_id: str
    completed: list[str] = field(default_factory=list)
    current: str | None = None
    finished: bool = False
    failed: str | None = None
    notes: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def path(run_dir: Path) -> Path:
        return run_dir / "coordinator_state.json"

    @classmethod
    def load(cls, run_dir: Path, run_id: str) -> "RunState":
        path = cls.path(run_dir)
        if path.exists():
            try:
                return cls(**json.loads(path.read_text()))
            except (ValueError, TypeError):
                pass            # corrupt state: start the stage list again; stages are idempotent
        return cls(run_id=run_id)

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path(run_dir).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2))
        os.replace(tmp, self.path(run_dir))
