"""Compact one-line renderings of steps and games, for prompts."""
from __future__ import annotations

from typing import Any


def event_text(event: dict[str, Any]) -> str:
    kind = event["type"]
    if kind == "played":
        return f"played({event['seat']},{event['card']})"
    if kind == "pressure":
        return f"pressure={event['value']}"
    if kind == "eliminated":
        return f"eliminated({event['seat']})"
    if kind == "revealed":
        return f"revealed({event['seat']},{event['cards']})"
    if kind == "drew":
        return f"drew({event['seat']},{event['card']})"
    if kind == "passed":
        return f"passed({event['seat']})"
    if kind == "turn_passed":
        return f"turn_passed(to={event['to']},actions={event['actions']},turn={event['turn']})"
    if kind == "game_over":
        return f"game_over(winner={event['winner']})"
    return str(event)


def action_text(action: dict[str, Any]) -> str:
    return f"play {action['card']}" if action["type"] == "play" else action["type"]


def step_line(step: dict[str, Any]) -> str:
    before, after = step["before"], step["view"]
    return (f"{step['trace_id']} seat{step['seat']} {action_text(step['action'])}"
            f" | P {before['pressure']}->{after['pressure']}"
            f" | hands {[len(h) for h in before['hands']]} pile {before['draw_pile_size']}"
            f" actions_left {before['actions_left']}"
            f" | " + " ".join(event_text(e) for e in step["events"]))


def game_digest(game, max_steps: int | None = None) -> str:
    head = (f"## game {game.game_id} players={game.players} label={game.label}\n"
            f"initial hands: {game.initial_view['hands']} pile={game.initial_view['draw_pile_size']}")
    steps = game.steps if max_steps is None else game.steps[:max_steps]
    return head + "\n" + "\n".join(step_line(s) for s in steps)
