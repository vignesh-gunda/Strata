"""Read the recovered partial code into claims and a list of unknowns.

Everything here comes from `ast`, with no model. We learn *structure*: what hidden
state exists, which parameters are read but never defined, which function bodies
were lost, which imported modules are missing. We do not trust it for rule values
(`rules.py` is absent), so no claim here states a number the code does not show.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from .specs import ClaimSpec

# GameState field -> the key the view uses for it (None = the view never shows it).
FIELD_VIEW: dict[str, str | None] = {
    "hands": "hands", "alive": "alive", "draw_pile": "draw_pile_size", "discard": "discard",
    "pressure": "pressure", "current": "current_seat", "actions_left": "actions_left",
    "turn": "turn", "over": "over", "winner": "winner",
    "jammed": None, "direction": None, "extra_actions": None, "skip_next": None,
}
_SKIPPED_FIELDS = {"rules"}  # holds the parameters, which are surveyed separately

# Subkind for claims the manual never mentions: leads that only the code gives us.
HINT = "hint_not_in_manual"

# (function, substrings that must all appear in its unparsed body, claim, hint_only)
_PATTERNS: list[tuple[str, tuple[str, ...], ClaimSpec, bool]] = [
    ("legal_actions", ("bool(state.draw_pile)", "len(hand) < state.rules.hand_limit"),
     ClaimSpec("draw_offered", "Draw is offered only when the draw pile is not empty and the "
               "hand holds fewer than rules.hand_limit cards.", "action:draw",
               {"type": "draw_offered", "needs_draw_pile": True, "hand_limit_param": "hand_limit"}),
     False),
    ("legal_actions", ("Action(PASS)", "actions or"),
     ClaimSpec("pass_only_when_stuck", "Pass is the only action when the player can neither play "
               "nor draw.", "action:pass", {"type": "pass_only_when_stuck"}), False),
    ("legal_actions", ("if state.over:", "return ()"),
     ClaimSpec("no_actions_when_over", "No actions are legal once the game is over.", "ending",
               {"type": "no_actions_when_over"}), True),
    ("step", ("action not in legal_actions(state)", "IllegalActionError"),
     ClaimSpec("illegal_rejected", "An action that is not in the legal actions is rejected.",
               "action:legal", {"type": "illegal_action_rejected"}), False),
    ("_scan", ("state.rules.scan_depth", "'revealed'"),
     ClaimSpec("scan_reveals", "Scan reveals the top rules.scan_depth cards of the draw pile to "
               "the player who played it (a 'revealed' event); the pile is unchanged.", "card:scan",
               {"type": "scan_reveals_top", "depth_param": "scan_depth"}), False),
    ("_is_stalemate", ("not state.draw_pile", "living_hands"),
     ClaimSpec("stalemate", "A stalemate is an empty draw pile with no cards in any living "
               "player's hand.", "ending:stalemate",
               {"type": "stalemate", "draw_pile_empty": True, "no_living_hands": True}), True),
    ("_end_game", ("living[0] if len(living) == 1 else None",),
     ClaimSpec("winner_null", "A game ends with winner null unless exactly one player is alive.",
               "ending:winner_null", {"type": "game_over_winner", "null_unless_one_alive": True}),
     True),
    ("_finish_action", ("sum(state.alive) <= 1 or _is_stalemate(state)",),
     ClaimSpec("game_ends", "The game ends when at most one player is alive or on a stalemate.",
               "ending", {"type": "game_ends_when", "alive_at_most": 1, "or_stalemate": True}),
     False),
    ("_finish_action", ("state.actions_left - 1 if state.alive[seat] else 0",),
     ClaimSpec("actions_decrement", "Each action uses one of the seat's actions; a seat that is "
               "eliminated loses its remaining actions.", "turn:actions",
               {"type": "actions_decrement_unless_eliminated"}), True),
    ("_advance_turn", ("2 if state.skip_next else 1",),
     ClaimSpec("skip_next", "A hidden skip_next flag makes the turn pass over one living seat.",
               "turn:skip", {"type": "skip_next_passes_two"}), True),
    ("_advance_turn", ("1 + state.extra_actions",),
     ClaimSpec("extra_actions", "The next seat gets 1 + extra_actions actions, then extra_actions "
               "resets to 0.", "turn:extra_actions",
               {"type": "next_actions", "formula": "1+extra_actions"}), False),
    ("_advance_turn", ("_next_alive(state.alive, seat, state.direction",),
     ClaimSpec("next_alive", "The turn passes to the next living seat in the current direction.",
               "turn:order", {"type": "turn_passes_to_next_alive"}), False),
    ("_advance_turn", ("state.turn + 1",),
     ClaimSpec("turn_counter", "The turn counter goes up by one on every handoff, not on every "
               "action.", "turn:counter", {"type": "turn_counter_per_handoff"}), True),
    ("_play", ("state.jammed[seat]", "_set_jam(base, seat, False)"),
     ClaimSpec("jam_clears", "If the player is jammed, the card they play is discarded with no "
               "effect and the jam is cleared.", "card:jam", {"type": "jam_wastes_next_card"}),
     False),
    ("_play", ("discard=state.discard + (card,)",),
     ClaimSpec("play_discards", "A played card goes to the discard pile.", "zone:discard",
               {"type": "played_card_discarded"}), True),
    ("new_state", ("dealt[seat::players]", "rules.hand_size"),
     ClaimSpec("deal_round_robin", "Cards are dealt round-robin from the top of the deck, "
               "rules.hand_size per player; the rest is the draw pile, top first.", "setup:deal",
               {"type": "deal_round_robin", "size_param": "hand_size"}), False),
    ("new_state", ("2 <= players <= 4",),
     ClaimSpec("players_2_4", "A game needs 2 to 4 players.", "setup:players",
               {"type": "players_range", "min": 2, "max": 4}), False),
    ("_next_alive", ("alive[position]",),
     ClaimSpec("skip_dead", "Eliminated seats are skipped when passing the turn.", "turn:order",
               {"type": "skip_eliminated"}), False),
]

# GameState defaults that hold at the start of every game, and whether the manual says so.
_DEFAULTS: dict[str, bool] = {"pressure": False, "current": True, "direction": True,
                              "actions_left": False, "turn": True}


@dataclass
class CodeSurvey:
    claims: list[tuple[ClaimSpec, bool]] = field(default_factory=list)   # (spec, hint_only)
    missing_functions: list[str] = field(default_factory=list)
    missing_modules: dict[str, list[str]] = field(default_factory=dict)
    parameters: list[str] = field(default_factory=list)
    hidden_state: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)

    def unknown_specs(self) -> list[ClaimSpec]:
        specs: list[ClaimSpec] = []
        for name in self.missing_functions:
            specs.append(ClaimSpec(
                f"missing_function:{name}",
                f"The behavior of {name} was lost: the recovered body only raises "
                "NotImplementedError.", f"function:{name}",
                {"type": "unknown_function", "function": name},
                kind="unknown", subkind="missing_function"))
        for module, names in self.missing_modules.items():
            specs.append(ClaimSpec(
                f"missing_module:{module}",
                f"Module {module} is imported but was not recovered "
                f"(it provides {', '.join(names)}).", f"module:{module}",
                {"type": "unknown_module", "module": module, "names": names},
                kind="unknown", subkind="missing_module"))
        for name in self.parameters:
            specs.append(ClaimSpec(
                f"parameter:{name}",
                f"rules.{name} is read by the code but its value is not in the recovered source.",
                f"param:{name}", {"type": "unknown_parameter", "name": name},
                kind="unknown", subkind="parameter"))
        for name in self.hidden_state:
            specs.append(ClaimSpec(
                f"hidden_state:{name}",
                f"GameState.{name} is hidden state: the view never shows it, so it can only be "
                "inferred from behavior.", f"hidden:{name}",
                {"type": "hidden_state", "field": name}, kind="unknown", subkind="hidden_state"))
        if {"Mutation", "ruleset_for"} & {n for names in self.missing_modules.values() for n in names}:
            specs.append(ClaimSpec(
                "rule_variation",
                "The engine builds its rules with ruleset_for(mutations), so rule values may "
                "differ between runs; what one run shows may not hold in another.",
                "rules:variation", {"type": "rule_variation"},
                kind="unknown", subkind="rule_variation"))
        return specs


def _is_lost(func: ast.FunctionDef) -> bool:
    body = list(func.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Raise):
        return False
    exc = body[0].exc
    target = exc.func if isinstance(exc, ast.Call) else exc
    return isinstance(target, ast.Name) and target.id == "NotImplementedError"


def survey_code(files: dict[str, str], view_keys: set[str] | None = None) -> CodeSurvey:
    """`files` maps a dig-site path (e.g. 'source/engine.py') to its text."""
    result = CodeSurvey()
    trees = {path: ast.parse(text, filename=path) for path, text in files.items()}
    recovered = {Path(path).stem for path in files}

    functions: dict[str, ast.FunctionDef] = {}
    parameters: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.setdefault(node.name, node)
            elif isinstance(node, ast.Attribute):
                owner = node.value
                if (isinstance(owner, ast.Name) and owner.id == "rules") or \
                        (isinstance(owner, ast.Attribute) and owner.attr == "rules"):
                    parameters.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("blackbox."):
                stem = node.module.split(".", 1)[1]
                if stem not in recovered:
                    names = result.missing_modules.setdefault(node.module, [])
                    names.extend(a.name for a in node.names if a.name not in names)

    result.functions = sorted(functions)
    result.missing_functions = [n for n, f in functions.items() if _is_lost(f)]
    result.parameters = sorted(parameters)

    bodies = {name: ast.unparse(func) for name, func in functions.items() if not _is_lost(func)}
    for function, needles, spec, hint in _PATTERNS:
        body = bodies.get(function)
        if body and all(n in body for n in needles):
            result.claims.append((spec, hint))

    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "GameState":
                defaults = {}
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        name = item.target.id
                        if name in _SKIPPED_FIELDS:
                            continue
                        view = FIELD_VIEW.get(name)
                        if view is None or (view_keys is not None and view not in view_keys):
                            result.hidden_state.append(name)
                        if isinstance(item.value, ast.Constant):
                            defaults[name] = item.value.value
                for name, hint in _DEFAULTS.items():
                    if name in defaults:
                        result.claims.append((ClaimSpec(
                            f"initial:{name}", f"A new game starts with {name} = {defaults[name]!r}.",
                            f"initial:{name}",
                            {"type": "initial_value", "field": name, "value": defaults[name]}), hint))
    return result
