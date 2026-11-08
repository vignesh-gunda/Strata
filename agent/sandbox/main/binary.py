"""Run the dig-site binary as a separate process and talk JSON lines to it.

The agent gets `start`, `send_request` and thin helpers. It never gets the
binary's bytes. The child runs with a scrubbed environment (no API keys) and
the dig-site root as its working directory.

Games live inside the child process, so `restart()` loses them.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

from .digsite_fs import DigSiteFS


class BinaryError(RuntimeError):
    pass


class BinaryCrashed(BinaryError):
    pass


class BinaryTimeout(BinaryError):
    pass


class BinaryRequestError(BinaryError):
    """The binary answered `ok: false`."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class BinaryProcess:
    def __init__(
        self,
        fs: DigSiteFS | None = None,
        *,
        python: str | None = None,
        timeout: float = 5.0,
        command: list[str] | None = None,  # test hook: run any JSON-lines program
    ):
        if command is None and fs is None:
            raise ValueError("need a DigSiteFS or an explicit command")
        self._fs = fs
        self._python = python or sys.executable
        self._command = command
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0

    # ----- lifecycle -----
    def start(self) -> "BinaryProcess":
        if self._proc is not None and self._proc.poll() is None:
            return self
        command = self._command or [self._python, str(self._fs._binary_path())]
        cwd = str(self._fs.root) if self._fs else None
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd, env=env,
        )
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True).start()
        return self

    @staticmethod
    def _pump(proc: subprocess.Popen, lines: "queue.Queue[str | None]") -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)  # EOF marker

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def restart(self) -> "BinaryProcess":
        self.close()
        return self.start()

    def __enter__(self) -> "BinaryProcess":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ----- requests -----
    def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._proc is None:
            raise BinaryError("binary not started; call start() first")
        self._next_id += 1
        request_id = self._next_id
        payload = json.dumps({"id": request_id, "method": method, "params": params or {}})
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._kill()
            raise BinaryCrashed("binary closed its input") from None
        try:
            line = self._lines.get(timeout=self.timeout)
        except queue.Empty:
            self._kill()
            raise BinaryTimeout(f"no reply to {method} within {self.timeout}s") from None
        if line is None:
            self._kill()
            raise BinaryCrashed("binary exited before replying")
        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            self._kill()
            raise BinaryCrashed("binary sent a non-JSON reply") from None
        if reply.get("id") != request_id:
            self._kill()
            raise BinaryCrashed("reply id did not match the request")
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise BinaryRequestError(error.get("kind", "unknown"), error.get("message", ""))
        return reply.get("result")

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()

    # ----- thin helpers over the contract -----
    def random_script(self, seed: int) -> dict[str, Any]:
        return self.send_request("random_script", {"seed": seed})

    def new_game(self, players: int, script: dict[str, Any]) -> dict[str, Any]:
        return self.send_request("new_game", {"players": players, "script": script})

    def legal_actions(self, game_id: str) -> list[dict[str, Any]]:
        return self.send_request("legal_actions", {"game_id": game_id})

    def act(self, game_id: str, action: dict[str, Any]) -> dict[str, Any]:
        return self.send_request("act", {"game_id": game_id, "action": action})

    def view(self, game_id: str) -> dict[str, Any]:
        return self.send_request("view", {"game_id": game_id})
