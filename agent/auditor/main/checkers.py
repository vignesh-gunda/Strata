"""Deterministic claim checkers. Pure code, no model.

Each checker takes a `FittingCorpus` (train games only) and a claim's rule and
returns a Verdict: supported, refuted, or undecided, with trace IDs.
  * One counterexample refutes a universal claim.
  * A claim is supported only if traces actively discriminate it.
  * Where hidden state (a jam, a reversed direction) could explain a step, the
    checker only uses steps where that hidden state is provably neutral.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .corpus import FittingCorpus, GameRecord

MAX_IDS = 5


@dataclass
class Verdict:
    outcome: str                                   # supported | refuted | undecided
    support: list[str] = field(default_factory=list)
    counter: list[str] = field(default_factory=list)
    detail: str = ""
    replacement: dict[str, Any] | None = None      # what the traces say instead


def _decide(support: list[str], counter: list[str], detail: str = "",
            replacement: dict[str, Any] | None = None) -> Verdict:
    if counter:
        return Verdict("refuted", support[:MAX_IDS], counter[:MAX_IDS],
                       f"{len(counter)} counterexample(s), {len(support)} supporting. {detail}".strip(),
                       replacement)
    if support:
        return Verdict("supported", support[:MAX_IDS], [],
                       f"{len(support)} supporting step(s), no counterexample. {detail}".strip())
    return Verdict("undecided", [], [], detail or "no step in the corpus tests this claim")


# ---------- small helpers ----------

def _first_event(events: list[dict], kind: str, after: str | None = None) -> dict | None:
    seen_marker = after is None
    for event in events:
        if not seen_marker:
            seen_marker = event["type"] == after
            continue
        if event["type"] == kind:
            return event
    return None


def _pressure_after_played(step: dict) -> dict | None:
    return _first_event(step["events"], "pressure", after="played")


def _plays(step: dict, card: str | None = None) -> bool:
    action = step["action"]
    return action.get("type") == "play" and (card is None or action.get("card") == card)


def _clean_steps(game: GameRecord) -> Iterator[dict]:
    """Steps up to the first jam play. After that, a hidden jam may confound a step."""
    for step in game.steps:
        yield step
        if _plays(step, "jam"):
            return


def _alive_count(view: dict) -> int:
    return sum(bool(a) for a in view["alive"])


def _next_alive(alive: list[bool], seat: int, direction: int) -> int:
    n = len(alive)
    position = seat
    for _ in range(n):
        position = (position + direction) % n
        if alive[position]:
            return position
    return seat


def _first_id(game: GameRecord) -> str | None:
    return game.steps[0]["trace_id"] if game.steps else None


# ---------- per-card pressure ----------

def check_pressure_delta(corpus: FittingCorpus, rule: dict) -> Verdict:
    card, value = rule["card"], rule["value"]
    support, counter = [], []
    unclamped: Counter = Counter()
    ids_for: dict[int, list[str]] = defaultdict(list)
    for _, step in corpus.steps():
        if not _plays(step, card):
            continue
        event = _pressure_after_played(step)
        if event is None:                     # wasted by a jam: no effect to judge
            continue
        before, observed = step["before"]["pressure"], event["value"]
        expected = before + value if value > 0 else max(0, before + value)
        meaningful = value > 0 or before + value > 0
        if observed == expected:
            if meaningful:
                support.append(step["trace_id"])
        else:
            counter.append(step["trace_id"])
        if observed > 0 or value > 0:
            delta = observed - before
            unclamped[delta] += 1
            ids_for[delta].append(step["trace_id"])
    replacement = None
    if counter and len(unclamped) == 1:
        (delta,) = unclamped
        replacement = {"card": card, "value": delta, "ids": ids_for[delta][:MAX_IDS]}
    elif counter and unclamped:
        delta, _count = unclamped.most_common(1)[0]
        if _count > 0.9 * sum(unclamped.values()):
            replacement = {"card": card, "value": delta, "ids": ids_for[delta][:MAX_IDS]}
    observed_note = f"observed deltas: {dict(unclamped)}" if unclamped else ""
    return _decide(support, counter, observed_note, replacement)


def check_pressure_floor(corpus: FittingCorpus, rule: dict) -> Verdict:
    card, floor = rule["card"], rule["value"]
    support, counter = [], []
    for _, step in corpus.steps():
        if not _plays(step, card):
            continue
        event = _pressure_after_played(step)
        if event is None:
            continue
        if event["value"] < floor:
            counter.append(step["trace_id"])
        elif event["value"] == floor and step["before"]["pressure"] > floor:
            support.append(step["trace_id"])
    return _decide(support, counter)


def check_scan_depth(corpus: FittingCorpus, rule: dict) -> Verdict:
    depth = rule["value"]
    support, counter = [], []
    lengths: Counter = Counter()
    ids_for: dict[int, list[str]] = defaultdict(list)
    for _, step in corpus.steps():
        event = _first_event(step["events"], "revealed")
        if event is None or step["before"]["draw_pile_size"] < depth:
            continue
        n = len(event["cards"])
        lengths[n] += 1
        ids_for[n].append(step["trace_id"])
        (support if n == depth else counter).append(step["trace_id"])
    replacement = None
    if counter and len(lengths) == 1:
        (n,) = lengths
        replacement = {"value": n, "ids": ids_for[n][:MAX_IDS]}
    return _decide(support, counter, f"revealed lengths: {dict(lengths)}" if lengths else "",
                   replacement)


# ---------- drawing and hand size ----------

def _draw_observations(corpus: FittingCorpus):
    """Yield (step, hand_size, pile, draw_offered) for every step."""
    for _, step in corpus.steps():
        before = step["before"]
        size = len(before["hands"][step["seat"]])
        offered = any(a["type"] == "draw" for a in step["legal"])
        yield step, size, before["draw_pile_size"], offered


def measure_hand_limit(corpus: FittingCorpus) -> dict[str, Any] | None:
    offered: dict[int, str] = {}
    withheld: dict[int, str] = {}
    for step, size, pile, draw in _draw_observations(corpus):
        if pile <= 0:
            continue
        (offered if draw else withheld).setdefault(size, step["trace_id"])
    if not offered or not withheld:
        return None
    limit = min(withheld)
    if max(offered) != limit - 1 or any(s < limit for s in withheld) or any(s >= limit for s in offered):
        return None                       # not a clean threshold
    return {"value": limit, "offered_at": limit - 1, "ids": [offered[limit - 1], withheld[limit]]}


def check_hand_limit(corpus: FittingCorpus, rule: dict) -> Verdict:
    limit = rule["value"]
    support, counter = [], []
    seen_below = seen_at = False
    for step, size, pile, draw in _draw_observations(corpus):
        if pile <= 0:
            continue
        if draw and size >= limit:
            counter.append(step["trace_id"])
        elif not draw and size < limit:
            counter.append(step["trace_id"])
        elif draw and size == limit - 1:
            seen_below = True
            support.append(step["trace_id"])
        elif not draw and size == limit:
            seen_at = True
            support.append(step["trace_id"])
    measured = measure_hand_limit(corpus)
    replacement = {"value": measured["value"], "ids": measured["ids"]} if counter and measured else None
    verdict = _decide(support, counter, f"measured limit: {measured['value']}" if measured else "",
                      replacement)
    if verdict.outcome == "supported" and not (seen_below and seen_at):
        verdict.outcome = "undecided"
        verdict.detail = "only one side of the threshold was observed"
    return verdict


def check_draw_offered(corpus: FittingCorpus, rule: dict) -> Verdict:
    """Draw needs a non-empty pile (the hand-limit half is an unknown parameter)."""
    support, counter = [], []
    saw_empty = saw_full = False
    for step, size, pile, draw in _draw_observations(corpus):
        if pile <= 0 and draw:
            counter.append(step["trace_id"])
        elif pile <= 0 and not draw:
            saw_empty = True
            support.append(step["trace_id"])
        elif pile > 0 and draw:
            saw_full = True
    verdict = _decide(support, counter)
    if verdict.outcome == "supported" and not saw_full:
        verdict.outcome = "undecided"
    return verdict


def check_pass_only_when_stuck(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for _, step in corpus.steps():
        legal = step["legal"]
        passes = [a for a in legal if a["type"] == "pass"]
        if passes:
            (support if len(legal) == 1 else counter).append(step["trace_id"])
        elif not legal:
            counter.append(step["trace_id"])
    return _decide(support, counter)


# ---------- setup (per game) ----------

def _per_game(corpus: FittingCorpus, test: Callable[[GameRecord], bool | None]) -> Verdict:
    support, counter = [], []
    for game in corpus.games():
        result = test(game)
        trace = _first_id(game)
        if result is None or trace is None:
            continue
        (support if result else counter).append(trace)
    return _decide(support, counter)


def check_deal_size(corpus: FittingCorpus, rule: dict) -> Verdict:
    return _per_game(corpus, lambda g: all(len(h) == rule["value"] for h in g.initial_view["hands"]))


def check_start_pressure(corpus: FittingCorpus, rule: dict) -> Verdict:
    return _per_game(corpus, lambda g: g.initial_view["pressure"] == rule["value"])


def check_players_range(corpus: FittingCorpus, rule: dict) -> Verdict:
    verdict = _per_game(corpus, lambda g: rule["min"] <= g.players <= rule["max"])
    if verdict.outcome == "supported":
        verdict.detail += " (only that observed players fall inside the range; bounds untested)"
    return verdict


_VIEW_FIELD = {"pressure": "pressure", "current": "current_seat", "actions_left": "actions_left",
               "turn": "turn"}


def check_initial_value(corpus: FittingCorpus, rule: dict) -> Verdict:
    view_key = _VIEW_FIELD.get(rule["field"])
    if view_key is None:
        return Verdict("undecided", detail="hidden field: the view does not show it")
    return _per_game(corpus, lambda g: g.initial_view[view_key] == rule["value"])


def check_deal_round_robin(corpus: FittingCorpus, rule: dict) -> Verdict:
    def test(game: GameRecord) -> bool:
        deck, view = game.script["deck"], game.initial_view
        n, players = len(view["hands"][0]), game.players
        return (all(view["hands"][s] == deck[s:n * players:players] for s in range(players))
                and view["draw_pile_size"] == len(deck) - n * players)
    return _per_game(corpus, test)


# ---------- overflow ----------

def _elimination_steps(corpus: FittingCorpus):
    for _, step in corpus.steps():
        if any(e["type"] == "eliminated" and e["seat"] == step["seat"] for e in step["events"]):
            yield step


def measure_pressure_limit(corpus: FittingCorpus) -> dict[str, Any] | None:
    eliminated, survived = {}, {}
    for _, step in corpus.steps():
        event = _pressure_after_played(step)
        if event is None:
            continue
        out = any(e["type"] == "eliminated" and e["seat"] == step["seat"] for e in step["events"])
        (eliminated if out else survived).setdefault(event["value"], step["trace_id"])
    if not eliminated or not survived or max(survived) != min(eliminated) - 1:
        return None
    limit = min(eliminated)
    return {"value": limit, "ids": [survived[limit - 1], eliminated[limit]]}


def check_pressure_limit(corpus: FittingCorpus, rule: dict) -> Verdict:
    limit = rule["value"]
    support, counter = [], []
    saw_out = saw_below = False
    for _, step in corpus.steps():
        event = _pressure_after_played(step)
        if event is None:
            continue
        out = any(e["type"] == "eliminated" and e["seat"] == step["seat"] for e in step["events"])
        value = event["value"]
        if out != (value >= limit):
            counter.append(step["trace_id"])
        elif out:
            saw_out = True
            support.append(step["trace_id"])
        elif value == limit - 1:
            saw_below = True
            support.append(step["trace_id"])
    measured = measure_pressure_limit(corpus)
    replacement = {"value": measured["value"], "ids": measured["ids"]} if counter and measured else None
    verdict = _decide(support, counter, f"measured limit: {measured['value']}" if measured else "",
                      replacement)
    if verdict.outcome == "supported" and not (saw_out and saw_below):
        verdict.outcome = "undecided"
    return verdict


def _overflow(test: Callable[[dict, int], bool]) -> Callable[[FittingCorpus, dict], Verdict]:
    def check(corpus: FittingCorpus, rule: dict) -> Verdict:
        support, counter = [], []
        for step in _elimination_steps(corpus):
            (support if test(step, step["seat"]) else counter).append(step["trace_id"])
        return _decide(support, counter)
    return check


check_overflow_eliminates = _overflow(lambda s, seat: not s["view"]["alive"][seat])
check_overflow_discards_hand = _overflow(lambda s, seat: s["view"]["hands"][seat] == [])
check_overflow_resets = _overflow(lambda s, seat: s["view"]["pressure"] == 0)


def check_overflow_passes_turn(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for step in _elimination_steps(corpus):
        if any(e["type"] == "game_over" for e in step["events"]):
            continue
        moved = _first_event(step["events"], "turn_passed")
        (support if moved and moved["to"] != step["seat"] else counter).append(step["trace_id"])
    return _decide(support, counter)


# ---------- endings ----------

def _game_over_steps(corpus: FittingCorpus):
    for game, step in corpus.steps():
        if any(e["type"] == "game_over" for e in step["events"]):
            yield game, step


def check_win_last_standing(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for _, step in _game_over_steps(corpus):
        alive = [i for i, a in enumerate(step["view"]["alive"]) if a]
        if len(alive) == 1:
            (support if step["view"]["winner"] == alive[0] else counter).append(step["trace_id"])
    return _decide(support, counter)


def no_winner_endings(corpus: FittingCorpus) -> list[str]:
    """Games that ended with several players alive and no winner (a stalemate)."""
    return [step["trace_id"] for _, step in _game_over_steps(corpus)
            if step["view"]["winner"] is None and _alive_count(step["view"]) > 1]


def _stalemate_now(view: dict) -> bool:
    living = [h for h, a in zip(view["hands"], view["alive"]) if a]
    return view["draw_pile_size"] == 0 and not any(living)


def check_stalemate(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for game, step in corpus.steps():
        view, over = step["view"], step["view"]["over"]
        if _stalemate_now(view) and _alive_count(view) > 1:
            (support if over and view["winner"] is None else counter).append(step["trace_id"])
        elif over and view["winner"] is None and _alive_count(view) > 1:
            counter.append(step["trace_id"])      # ended without winner but no stalemate
    return _decide(support, counter)


def check_game_over_winner(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for _, step in _game_over_steps(corpus):
        view = step["view"]
        alive = [i for i, a in enumerate(view["alive"]) if a]
        expected = alive[0] if len(alive) == 1 else None
        (support if view["winner"] == expected else counter).append(step["trace_id"])
    return _decide(support, counter)


def check_game_ends_when(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for _, step in corpus.steps():
        view = step["view"]
        should_end = _alive_count(view) <= 1 or _stalemate_now(view)
        if should_end == view["over"]:
            if should_end:
                support.append(step["trace_id"])
        else:
            counter.append(step["trace_id"])
    return _decide(support, counter)


# ---------- hidden-state-dependent claims (clean steps only) ----------

def check_next_player_actions(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for game in corpus.games():
        for step in _clean_steps(game):
            if not _plays(step, "double"):
                continue
            moved = _first_event(step["events"], "turn_passed")
            if moved is None:
                continue
            (support if moved["actions"] == rule["value"] else counter).append(step["trace_id"])
    return _decide(support, counter)


def check_reverse(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for game in corpus.games():
        if game.players < 3:
            continue
        direction = 1
        for step in _clean_steps(game):
            if _plays(step, "reverse"):
                direction = -direction
            moved = _first_event(step["events"], "turn_passed")
            if moved is None:
                continue
            alive, seat = step["view"]["alive"], step["seat"]
            expected = _next_alive(alive, seat, direction)
            if moved["to"] != expected:
                counter.append(step["trace_id"])
            elif direction == -1 and _next_alive(alive, seat, 1) != expected:
                support.append(step["trace_id"])
    return _decide(support, counter)


_VISIBLE = {"gear1": "pressure", "gear2": "pressure", "gear3": "pressure", "vent": "pressure",
            "scan": "revealed"}


def check_jam(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for game in corpus.games():
        jams = [s for s in game.steps if _plays(s, "jam")]
        if len(jams) != 1:
            continue                       # chains of jams are too ambiguous to judge
        jam = jams[0]
        moved = _first_event(jam["events"], "turn_passed")
        if moved is None:
            continue
        target, judged_first = moved["to"], False
        for step in game.steps[game.steps.index(jam) + 1:]:
            if step["seat"] != target or not _plays(step):
                continue
            kind = _VISIBLE.get(step["action"]["card"])
            if kind is None:
                break              # the very next card has no visible effect: cannot judge
            has_effect = _first_event(step["events"], kind, after="played") is not None
            if not judged_first:
                (counter if has_effect else support).append(step["trace_id"])
                judged_first = True
            else:                  # the jam is used up: the card after that must work again
                (support if has_effect else counter).append(step["trace_id"])
                break
    return _decide(support, counter)


def _pile_walk(game: GameRecord):
    """Yield (step, pile_before) while tracking the draw pile from the script."""
    deck = list(game.script["deck"])
    n = len(game.initial_view["hands"][0])
    pile = deck[n * game.players:]
    for step in game.steps:
        yield step, list(pile)
        for event in step["events"]:
            if event["type"] == "drew":
                if not pile or pile[0] != event["card"]:
                    return                 # lost track of the pile: stop trusting it
                pile.pop(0)


def check_scan_reveals_top(corpus: FittingCorpus, rule: dict) -> Verdict:
    support, counter = [], []
    for game in corpus.games():
        for step, pile in _pile_walk(game):
            event = _first_event(step["events"], "revealed")
            if event is None:
                continue
            cards = event["cards"]
            (support if cards == pile[:len(cards)] else counter).append(step["trace_id"])
    return _decide(support, counter)


CHECKERS: dict[str, Callable[[FittingCorpus, dict], Verdict]] = {
    "pressure_delta": check_pressure_delta,
    "pressure_floor": check_pressure_floor,
    "scan_depth": check_scan_depth,
    "hand_limit": check_hand_limit,
    "draw_offered": check_draw_offered,
    "pass_only_when_stuck": check_pass_only_when_stuck,
    "deal_size": check_deal_size,
    "start_pressure": check_start_pressure,
    "players_range": check_players_range,
    "initial_value": check_initial_value,
    "deal_round_robin": check_deal_round_robin,
    "pressure_limit": check_pressure_limit,
    "overflow_eliminates": check_overflow_eliminates,
    "overflow_discards_hand": check_overflow_discards_hand,
    "overflow_resets_pressure": check_overflow_resets,
    "overflow_passes_turn": check_overflow_passes_turn,
    "win_last_standing": check_win_last_standing,
    "stalemate": check_stalemate,
    "game_over_winner": check_game_over_winner,
    "game_ends_when": check_game_ends_when,
    "next_player_actions": check_next_player_actions,
    "reverse_flips_direction": check_reverse,
    "jam_wastes_next_card": check_jam,
    "scan_reveals_top": check_scan_reveals_top,
}
