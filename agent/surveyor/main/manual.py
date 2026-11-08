"""Turn the player's manual into Documented-tier claims.

Deterministic on purpose: the same manual always yields the same claims, so the
corpus checker (T4) can rely on each claim's machine-readable `rule`.
Rows or sentences the parser does not understand still become claims (with no
rule) so nothing in the manual is silently dropped.
"""
from __future__ import annotations

import re

from .specs import ClaimSpec


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^##\s+\d+\.\s+(.+)$", text, flags=re.MULTILINE)
    return {parts[i].strip().lower(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _card_id(name: str) -> str:
    return re.sub(r"\s+", "", name.strip().lower())


def _card_specs(card: str, name: str, desc: str) -> list[ClaimSpec]:
    topic = f"card:{card}"
    specs: list[ClaimSpec] = []

    def add(suffix: str, text: str, rule: dict) -> None:
        specs.append(ClaimSpec(f"card:{card}:{suffix}", text, topic, {"card": card, **rule}))

    if m := re.search(r"Add (\d+) to the pressure", desc):
        add("pressure_delta", f"{name} adds {m[1]} to the pressure gauge.",
            {"type": "pressure_delta", "value": int(m[1])})
    if m := re.search(r"Lower the pressure gauge by (\d+)", desc):
        add("pressure_delta", f"{name} lowers the pressure gauge by {m[1]}.",
            {"type": "pressure_delta", "value": -int(m[1])})
        if f := re.search(r"never goes below (\d+)", desc):
            add("pressure_floor", f"Pressure never goes below {f[1]} after {name}.",
                {"type": "pressure_floor", "value": int(f[1])})
    if re.search(r"Reverse the direction", desc):
        add("reverse", f"{name} reverses the direction of play.", {"type": "reverse_flips_direction"})
    if m := re.search(r"next player takes (\d+) actions", desc):
        add("double", f"After {name}, the next player takes {m[1]} actions on their turn.",
            {"type": "next_player_actions", "value": int(m[1])})
    if re.search(r"Jam the next player", desc) and "wasted" in desc:
        add("jam", f"{name} jams the next player: their next card is wasted "
            "(discarded, no effect).", {"type": "jam_wastes_next_card"})
    if m := re.search(r"top (\d+) cards of the draw pile", desc):
        add("scan_depth", f"{name} shows the top {m[1]} cards of the draw pile.",
            {"type": "scan_depth", "value": int(m[1])})
    if not specs:
        specs.append(ClaimSpec(f"card:{card}:raw", f"{name}: {desc.strip()}", topic))
    return specs


def parse_manual(text: str) -> list[ClaimSpec]:
    sections = _sections(text)
    specs: list[ClaimSpec] = []

    overview = sections.get("overview", text)
    if m := re.search(r"(\d+) to (\d+) players", overview):
        specs.append(ClaimSpec("setup:players", f"The game is for {m[1]} to {m[2]} players.",
                               "setup:players",
                               {"type": "players_range", "min": int(m[1]), "max": int(m[2])}))

    setup = sections.get("setup", "")
    if m := re.search(r"Deal (\d+) cards to each player", setup):
        specs.append(ClaimSpec("setup:deal", f"Each player is dealt {m[1]} cards.",
                               "setup:deal", {"type": "deal_size", "value": int(m[1])}))
    if m := re.search(r"pressure gauge to (\d+)", setup):
        specs.append(ClaimSpec("setup:start_pressure", f"The pressure gauge starts at {m[1]}.",
                               "setup:pressure", {"type": "start_pressure", "value": int(m[1])}))

    turn = sections.get("taking a turn", "")
    if re.search(r"you get one action", turn, re.IGNORECASE):
        specs.append(ClaimSpec("turn:one_action", "A player gets one action per turn.",
                               "turn:actions", {"type": "actions_per_turn", "value": 1}))
    if m := re.search(r"hold up to (\d+) cards", turn):
        specs.append(ClaimSpec("turn:hand_limit", f"A player may hold up to {m[1]} cards.",
                               "action:draw", {"type": "hand_limit", "value": int(m[1])}))
    if re.search(r"Pass\*\*, but only if you cannot play or draw", turn):
        specs.append(ClaimSpec("turn:pass_only_when_stuck",
                               "A player may pass only if they cannot play or draw.",
                               "action:pass", {"type": "pass_only_when_stuck"}))
    if re.search(r"Play then moves to the next player", turn):
        specs.append(ClaimSpec("turn:next_player", "After an action, play moves to the next player.",
                               "turn:order", {"type": "turn_passes_to_next_player"}))

    for line in sections.get("the cards", "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or len(cells) < 2:
            continue
        if cells[0].lower() == "card" or set(cells[0]) <= set("-: "):
            continue
        specs.extend(_card_specs(_card_id(cells[0]), cells[0], cells[1]))

    overflow = sections.get("overflow", "")
    if m := re.search(r"limit is (\d+)", overflow):
        specs.append(ClaimSpec("overflow:limit",
                               f"The pressure limit is {m[1]}; playing a card that brings "
                               f"pressure to {m[1]} or more makes the player overflow.",
                               "overflow", {"type": "pressure_limit", "value": int(m[1])}))
    if "You are out of the game" in overflow:
        specs.append(ClaimSpec("overflow:eliminated", "A player who overflows is out of the game.",
                               "overflow", {"type": "overflow_eliminates"}))
    if "hand is discarded" in overflow:
        specs.append(ClaimSpec("overflow:discard_hand", "An overflowing player's hand is discarded.",
                               "overflow", {"type": "overflow_discards_hand"}))
    if m := re.search(r"gauge resets to (\d+)", overflow):
        specs.append(ClaimSpec("overflow:reset", f"After an overflow the gauge resets to {m[1]}.",
                               "overflow", {"type": "overflow_resets_pressure", "value": int(m[1])}))
    if re.search(r"Play moves to the next player", overflow):
        specs.append(ClaimSpec("overflow:next_player", "After an overflow, play moves to the next player.",
                               "overflow", {"type": "overflow_passes_turn"}))

    if re.search(r"only one player remains, that player wins", sections.get("winning", ""), re.IGNORECASE):
        specs.append(ClaimSpec("win:last_standing", "When only one player remains, that player wins.",
                               "ending", {"type": "win_last_standing"}))
    return specs
