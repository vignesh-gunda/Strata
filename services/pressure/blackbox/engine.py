"""Pure game engine: ``step(state, action) -> (new_state, events)``.

Nothing here mutates its inputs; every transition returns a new state.
Events only describe what an observer could see; causes stay hidden.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Sequence

from blackbox.cards import CARD_ORDER, GEAR_HEAT, Card
from blackbox.rules import Mutation, ruleset_for
from blackbox.state import DRAW, PASS, PLAY, Action, GameState

Event = dict
Events = tuple[Event, ...]
Effect = Callable[[GameState, int, Card], tuple[GameState, Events]]


SUDDEN_DEATH_TURN = 200  # a game that reaches this turn ends at once


class IllegalActionError(ValueError):
    """The action is well formed but not allowed in the current state."""


def new_state(players: int, deck: Sequence[Card], mutations: Iterable[Mutation] = ()) -> GameState:
    if not 2 <= players <= 4:
        raise ValueError(f"players must be 2-4, got {players}")
    rules = ruleset_for(mutations)
    needed = players * rules.hand_size
    if len(deck) < needed:
        raise ValueError(f"deck needs at least {needed} cards for {players} players, got {len(deck)}")
    dealt = tuple(deck[:needed])
    return GameState(
        rules=rules,
        hands=tuple(dealt[seat::players] for seat in range(players)),
        alive=(True,) * players,
        draw_pile=tuple(deck[needed:]),
        discard=(),
        jammed=(False,) * players,
    )


def legal_actions(state: GameState) -> tuple[Action, ...]:
    if state.over:
        return ()
    hand = state.hands[state.current]
    plays = tuple(Action(PLAY, card) for card in CARD_ORDER if card in hand)
    can_draw = (bool(state.draw_pile) or len(state.discard) > 1) and len(hand) < state.rules.hand_limit
    actions = plays + ((Action(DRAW),) if can_draw else ())
    return actions or (Action(PASS),)


def step(state: GameState, action: Action) -> tuple[GameState, Events]:
    if action not in legal_actions(state):
        raise IllegalActionError("illegal action in the current state")
    seat = state.current
    if action.kind == PLAY:
        after, events = _play(state, seat, action.card)
    elif action.kind == DRAW:
        after, events = _draw(state, seat)
    else:
        after, events = state, ({"type": "passed", "seat": seat},)
    return _finish_action(after, seat, events)


# --- helpers ---------------------------------------------------------------


def _replace_at(items: tuple, index: int, value) -> tuple:
    return items[:index] + (value,) + items[index + 1 :]


def _remove_one(hand: tuple[Card, ...], card: Card) -> tuple[Card, ...]:
    index = hand.index(card)
    return hand[:index] + hand[index + 1 :]


def _next_alive(alive: tuple[bool, ...], seat: int, direction: int, steps: int) -> int:
    position, moved = seat, 0
    while moved < steps:
        position = (position + direction) % len(alive)
        moved += 1 if alive[position] else 0
    return position


def _set_jam(state: GameState, seat: int, value: bool) -> GameState:
    return replace(state, jammed=_replace_at(state.jammed, seat, value))


# --- card effects ----------------------------------------------------------


def _gear(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    pressure = state.pressure + GEAR_HEAT[card]
    raised = ({"type": "pressure", "value": pressure},)
    if pressure < state.rules.pressure_limit:
        return replace(state, pressure=pressure), raised
    reset = state.rules.overflow_reset
    eliminated = replace(
        state,
        alive=_replace_at(state.alive, seat, False),
        hands=_replace_at(state.hands, seat, ()),
        discard=state.discard + state.hands[seat],
        jammed=_replace_at(state.jammed, seat, False),
        pressure=reset,
    )
    return eliminated, raised + ({"type": "eliminated", "seat": seat}, {"type": "pressure", "value": reset})


def _vent(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    pressure = max(0, state.pressure - state.rules.vent_amount)
    return replace(state, pressure=pressure), ({"type": "pressure", "value": pressure},)


def _reverse(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    skip = state.skip_next or state.rules.reverse_skips
    return replace(state, direction=-state.direction, skip_next=skip), ()


def _double(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    extra = state.extra_actions + 1 if state.rules.stacking_double else max(state.extra_actions, 1)
    return replace(state, extra_actions=extra), ()


def _jam(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    target = _next_alive(state.alive, seat, state.direction, 1)
    return _set_jam(state, target, True), ()


def _scan(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    seen = [top.value for top in state.draw_pile[: state.rules.scan_depth]]
    return state, ({"type": "revealed", "seat": seat, "cards": seen},)


_EFFECTS: dict[Card, Effect] = {
    Card.GEAR1: _gear,
    Card.GEAR2: _gear,
    Card.GEAR3: _gear,
    Card.VENT: _vent,
    Card.REVERSE: _reverse,
    Card.DOUBLE: _double,
    Card.JAM: _jam,
    Card.SCAN: _scan,
}


# --- actions and turn flow -------------------------------------------------


def _play(state: GameState, seat: int, card: Card) -> tuple[GameState, Events]:
    base = replace(
        state,
        hands=_replace_at(state.hands, seat, _remove_one(state.hands[seat], card)),
        discard=state.discard + (card,),
    )
    played = ({"type": "played", "seat": seat, "card": card.value},)
    if state.jammed[seat]:  # the card is spent with no visible effect; agents must infer why
        return _set_jam(base, seat, False), played
    after, effect_events = _EFFECTS[card](base, seat, card)
    return after, played + effect_events


def _draw(state: GameState, seat: int) -> tuple[GameState, Events]:
    if state.jammed[seat] and state.rules.jam_blocks_draw:  # the draw silently does nothing
        return _set_jam(state, seat, False), ()
    recycled: Events = ()
    if not state.draw_pile:  # empty pile: the discard pile (all but its top card) becomes the new pile
        state = replace(state, draw_pile=state.discard[:-1], discard=state.discard[-1:])
        recycled = ({"type": "recycled", "count": len(state.draw_pile)},)
    card = state.draw_pile[0]
    after = replace(
        state,
        hands=_replace_at(state.hands, seat, state.hands[seat] + (card,)),
        draw_pile=state.draw_pile[1:],
    )
    return after, recycled + ({"type": "drew", "seat": seat, "card": card.value},)


def _is_stalemate(state: GameState) -> bool:
    living_hands = (hand for hand, alive in zip(state.hands, state.alive) if alive)
    no_cards_left = not state.draw_pile and len(state.discard) <= 1 and not any(living_hands)
    return no_cards_left or state.turn >= SUDDEN_DEATH_TURN


def _end_game(state: GameState, events: Events) -> tuple[GameState, Events]:
    """There is always a winner: the last seat standing, or else the living seat
    holding the fewest cards (lowest seat number breaks a tie)."""
    living = [seat for seat, alive in enumerate(state.alive) if alive]
    winner = min(living, key=lambda seat: (len(state.hands[seat]), seat))
    return replace(state, over=True, winner=winner), events + ({"type": "game_over", "winner": winner},)


def _advance_turn(state: GameState, seat: int, events: Events) -> tuple[GameState, Events]:
    steps = 2 if state.skip_next else 1
    target = _next_alive(state.alive, seat, state.direction, steps)
    actions = 1 + state.extra_actions
    turn = state.turn + 1
    after = replace(state, current=target, actions_left=actions, extra_actions=0, skip_next=False, turn=turn)
    return after, events + ({"type": "turn_passed", "to": target, "actions": actions, "turn": turn},)


def _finish_action(state: GameState, seat: int, events: Events) -> tuple[GameState, Events]:
    if sum(state.alive) <= 1 or _is_stalemate(state):
        return _end_game(state, events)
    remaining = state.actions_left - 1 if state.alive[seat] else 0
    if remaining > 0:
        return replace(state, actions_left=remaining), events
    return _advance_turn(state, seat, events)
