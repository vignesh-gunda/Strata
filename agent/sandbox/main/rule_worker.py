"""Run model-written rule code in a child process with no API keys and limits.

    worker = RuleWorker()
    meta = worker.load(code)                      # {"kind": ..., "covers": [...]}
    results = worker.evaluate_game(steps)         # [{"p": ...} | {"err": ...}, ...]

Reuses BinaryProcess for the JSON-lines plumbing, timeouts and crash handling.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .binary import BinaryProcess, BinaryRequestError, BinaryTimeout

_WORKER = str(Path(__file__).with_name("_rule_worker_main.py"))


class RuleError(RuntimeError):
    """The rule code could not be loaded (syntax error, bad KIND, forbidden import...)."""


class RuleTimeout(RuleError):
    """The rule ran too long and its process was killed."""


class RuleWorker:
    def __init__(self, *, timeout: float = 10.0, cpu_seconds: int = 20):
        self._proc = BinaryProcess(
            command=[sys.executable, "-I", _WORKER, str(cpu_seconds)], timeout=timeout)
        self.meta: dict[str, Any] | None = None

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self._proc.send_request(method, params)
        except BinaryTimeout as error:
            raise RuleTimeout(f"rule exceeded its time limit: {error}") from None
        except BinaryRequestError as error:
            raise RuleError(error.message) from None

    def load(self, code: str) -> dict[str, Any]:
        self._proc.start()
        self.meta = self._call("load", {"code": code})
        return self.meta

    def evaluate_game(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._call("evaluate_game", {"steps": steps})

    def evaluate_setup(self, games: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._call("evaluate_setup", {"games": games})

    def live_reset(self) -> None:
        self._call("live_reset")

    def live_predict(self, step: dict[str, Any]) -> dict[str, Any]:
        return self._call("live_predict", {"step": step})

    def live_observe(self, step: dict[str, Any]) -> None:
        self._call("live_observe", {"step": step})

    def close(self) -> None:
        self._proc.close()

    def __enter__(self) -> "RuleWorker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
