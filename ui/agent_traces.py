"""T16.3: turn the agent's own probe games into the events the game table already understands.

The agent writes one finished game per line to `runs/<run_id>/probe_games.jsonl`. The table
listens for three events: `game` (a game starts), `trace` (one step) and `end` (it finished).
This module reads new lines as they appear and maps each game to those events, so the table
shows what the agent really played and not the built-in simulator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def game_events(game: dict, index: int) -> list[tuple[str, dict]]:
    """One probe game as ("game", ...), ("trace", ...)*, ("end", ...) in the table's shape."""
    steps = [step for step in game.get("steps", []) if isinstance(step, dict)]
    last_view = steps[-1].get("view", {}) if steps else game.get("initial_view", {})
    winner = last_view.get("winner") if isinstance(last_view, dict) else None
    events: list[tuple[str, dict]] = [("game", {
        "game_id": game.get("game_id"), "index": index, "label": game.get("label", "probe"),
        "split": game.get("split", "train"), "players": game.get("players"),
        "view": game.get("initial_view", {}),
    })]
    events.extend(("trace", step) for step in steps)
    events.append(("end", {"game_id": game.get("game_id"), "index": index, "steps": len(steps), "winner": winner}))
    return events


class GameTail:
    """Reads complete new lines from the games file, remembering how far it got."""

    def __init__(self, path: Path, skip: int = 0):
        self.path = Path(path)
        self.skip = max(0, skip)       # games already delivered (from Last-Event-ID)
        self.seen = 0                  # complete lines passed so far, delivered or skipped
        self._offset = 0

    def poll(self) -> Iterator[tuple[int, dict]]:
        """Yield (index, game) for each complete new game line."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self._offset:        # file was rewritten: start over
            self._offset, self.seen = 0, 0
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return                      # nothing complete yet (a line is still being written)
        self._offset += end + 1
        for raw in chunk[: end + 1].splitlines():
            index = self.seen
            self.seen += 1
            if index < self.skip or not raw.strip():
                continue
            try:
                game = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(game, dict):
                yield index, game
