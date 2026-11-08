"""Client the agents use: it starts the black box in a separate process.

    with BlackBoxClient(seed=42, mutations=2) as box:
        game = box.new_game(3, script_from_seed(7))
        box.act(game["game_id"], box.legal_actions(game["game_id"])[0])
"""

from __future__ import annotations

import json
import select
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 10.0


class BlackBoxError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class BlackBoxClient:
    def __init__(
        self,
        seed: int = 0,
        mutations: int = 0,
        mutation_names: Sequence[str] = (),
        opaque: bool = False,
        allow_reveal: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        command: Sequence[str] | None = None,
    ) -> None:
        """``command`` runs any program that speaks the protocol instead, such as the
        dig-site binary or a rebuild. The game flags are then ignored, and the
        program gets no PYTHONPATH, so it must stand on its own."""
        env = {"PATH": "/usr/bin:/bin"}  # no API keys reach the black box
        if command is None:
            command = [sys.executable, "-m", "blackbox.server", "--seed", str(seed), "--mutations", str(mutations)]
            command += [arg for name in mutation_names for arg in ("--mutation", name)]
            command += ["--opaque"] if opaque else []
            command += ["--allow-reveal"] if allow_reveal else []
            env["PYTHONPATH"] = str(REPO_ROOT)
        self._timeout = timeout
        self._next_id = 0
        self._process = subprocess.Popen(
            list(command),
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def __enter__(self) -> "BlackBoxClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.stdin.close()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        for stream in (self._process.stdout, self._process.stderr):
            stream.close()

    def new_game(self, players: int, script: dict) -> dict:
        return self._call("new_game", players=players, script=script)

    def random_script(self, seed: int) -> dict:
        return self._call("random_script", seed=seed)

    def legal_actions(self, game_id: str) -> list[dict]:
        return self._call("legal_actions", game_id=game_id)

    def act(self, game_id: str, action: dict) -> dict:
        return self._call("act", game_id=game_id, action=action)

    def view(self, game_id: str) -> dict:
        return self._call("view", game_id=game_id)

    def reveal(self) -> dict:
        return self._call("reveal")

    def _call(self, method: str, **params: Any) -> Any:
        if self._process.poll() is not None:
            raise BlackBoxError("process_exited", self._process.stderr.read().decode(errors="replace"))
        self._next_id += 1
        request = {"id": self._next_id, "method": method, "params": params}
        self._process.stdin.write((json.dumps(request) + "\n").encode())
        self._process.stdin.flush()
        response = self._read_reply(method, self._next_id)
        if not response.get("ok"):
            error = response.get("error", {})
            raise BlackBoxError(error.get("kind", "unknown"), error.get("message", ""))
        return response["result"]

    def _read_reply(self, method: str, request_id: int) -> dict:
        """Read lines until the reply for ``request_id``; late replies to timed-out calls are dropped."""
        while True:
            ready, _, _ = select.select([self._process.stdout], [], [], self._timeout)
            if not ready:
                raise BlackBoxError("timeout", f"no reply to {method} within {self._timeout}s")
            line = self._process.stdout.readline()
            if not line:
                raise BlackBoxError("process_exited", self._process.stderr.read().decode(errors="replace"))
            response = json.loads(line)
            if response.get("id") in (request_id, None):
                return response
