"""Tiny builders for hand-made games, used by tests across components."""
from __future__ import annotations

from typing import Any

from .corpus import GameRecord, _add_before


def make_view(players: int = 2, hands: list[list[str]] | None = None,
              alive: list[bool] | None = None, pile: int = 10, current: int = 0,
              actions_left: int = 1, pressure: int = 0, turn: int = 1, over: bool = False,
              winner: int | None = None, discard: list[str] | None = None) -> dict[str, Any]:
    return {"players": players, "hands": hands if hands is not None else [["vent"] * 4] * players,
            "alive": alive if alive is not None else [True] * players,
            "discard": discard or [], "draw_pile_size": pile, "current_seat": current,
            "actions_left": actions_left, "pressure": pressure, "turn": turn, "over": over,
            "winner": winner}


def make_game(game_id: str, steps: list[dict[str, Any]], *, split: str = "train",
              origin: str = "corpus", players: int = 2, deck: list[str] | None = None,
              initial: dict[str, Any] | None = None, prefix: str = "t",
              start: int = 1, label: str = "synthetic", meta: dict | None = None) -> GameRecord:
    """Each step dict: seat, action, events, view (full after-view), legal (optional)."""
    initial = initial or make_view(players)
    built = []
    for n, spec in enumerate(steps):
        built.append({
            "trace_id": f"{prefix}-{start + n:06d}", "run_id": "r-test", "game_id": game_id,
            "step": n + 1, "seat": spec.get("seat", 0), "turn": spec.get("turn", n + 1),
            "action": spec["action"], "legal": spec.get("legal", [spec["action"]]),
            "events": spec.get("events", []), "view": spec["view"], "split": split,
            "label": label, "prediction": None, "surprise": None})
    game = GameRecord(game_id=game_id, players=players,
                      script={"deck": deck or ["vent"] * 42}, initial_view=initial, split=split,
                      label=label, steps=built, origin=origin, meta=meta or {})
    _add_before(game)
    return game
