"""JSONL trace recorder. Every step becomes one line with a stable trace ID."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO


class TraceRecorder:
    """Streams trace and game records to ``traces.jsonl`` and ``games.jsonl``."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir
        self.run_id = run_id
        self.trace_count = 0
        self._traces: IO[str] = (run_dir / "traces.jsonl").open("w", encoding="utf-8")
        self._games: IO[str] = (run_dir / "games.jsonl").open("w", encoding="utf-8")

    def __enter__(self) -> "TraceRecorder":
        return self

    def __exit__(self, *exc_info) -> None:
        self._traces.close()
        self._games.close()

    def trace(self, record: dict) -> dict:
        """Write one step and return the full line. ``prediction``/``surprise`` stay null for a Prober."""
        self.trace_count += 1
        line = {
            "trace_id": f"t-{self.trace_count:06d}",
            "run_id": self.run_id,
            **record,
            "prediction": None,
            "surprise": None,
        }
        self._traces.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._traces.flush()  # so `tail -f traces.jsonl` shows every step immediately
        return line

    def game(self, record: dict) -> dict:
        line = {"run_id": self.run_id, **record}
        self._games.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._games.flush()
        return line

    def manifest(self, record: dict) -> None:
        (self.run_dir / "manifest.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
