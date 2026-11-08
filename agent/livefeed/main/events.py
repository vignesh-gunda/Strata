"""T15.1: the append-only event log, `runs/<run_id>/events.jsonl`.

One event per agent action. The log is the single source for the live feed (terminal,
chat and the UI app), so a line can only describe something that was recorded here.

Several processes write to it over a run (each pipeline stage is its own process), so
every append takes a file lock and reads the last loop number under that lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from . import safety

EVENTS_FILE = "events.jsonl"

# What the agent can do. Each type has a sentence template in `lines.py`.
EVENT_TYPES = ("survey", "audit", "probe", "hypothesize", "score", "rebuild", "verify", "repair")

_TAIL_BYTES = 64 * 1024


class Event(BaseModel):
    loop: int                                   # 1, 2, 3... one per agent action
    at: str
    type: str
    actor: str = "agent"
    data: dict[str, Any] = Field(default_factory=dict)
    trace_ids: list[str] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def events_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / EVENTS_FILE


def _last_loop(handle) -> int:
    """Highest loop number in the file, found from the tail. Skips a torn last line."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - _TAIL_BYTES))
    chunk = handle.read()
    for line in reversed(chunk.splitlines()):
        try:
            return int(json.loads(line)["loop"])
        except (ValueError, KeyError, TypeError):
            continue
    return 0


class EventLog:
    def __init__(self, run_dir: str | Path):
        self.path = events_path(run_dir)
        self._lock = threading.Lock()

    def emit(self, type: str, data: dict[str, Any] | None = None, *,
             trace_ids: Iterable[str] = (), actor: str = "agent") -> Event:
        """Append one event and return it. Raises ValueError for an unknown type."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        data = dict(data or {})
        ids = [str(t) for t in trace_ids]
        if not safety.is_safe(data, ids, actor):  # restricted, secret or raw model text is never stored
            data, ids = {"redacted": True}, []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                event = Event(loop=_last_loop(handle) + 1, at=_now(), type=type, actor=actor,
                              data=data, trace_ids=ids)
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True,
                                        default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        return event

    def read(self, after: int = 0) -> list[Event]:
        return read_events(self.path.parent, after)

    def last_loop(self) -> int:
        events = self.read()
        return events[-1].loop if events else 0


def read_events(run_dir: str | Path, after: int = 0) -> list[Event]:
    """Events with loop > `after`, in order. Unreadable lines are skipped, not fatal."""
    path = events_path(run_dir)
    if not path.exists():
        return []
    found: list[Event] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = Event.model_validate_json(line)
            except ValueError:
                continue                        # a half-written final line, read again next time
            if event.loop > after:
                found.append(event)
    return found


def safe_emit(log: EventLog | None, type: str, data: dict[str, Any] | None = None, *,
              trace_ids: Iterable[str] = ()) -> None:
    """Used from pipeline code: a logging problem must never stop the agent."""
    if log is None:
        return
    try:
        log.emit(type, data, trace_ids=trace_ids)
    except Exception:
        pass
