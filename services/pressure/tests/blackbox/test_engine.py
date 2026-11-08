"""Behavioural tests for the hidden black-box game engine."""

from dataclasses import replace

import pytest

from blackbox.cards import Card
from blackbox.engine import IllegalActionError, legal_actions, new_state, step
from blackbox.rules import Mutation
from blackbox.scripts import stacked_script
from blackbox.state import Action

FILL = ["vent", "vent", "vent", "vent"]
DRAW = Action("draw")
PASS = Action("pass")


def make(hands, pile=("vent",), mutations=()):
    script = stacked_script(hands, pile)
    deck = [Card(name) for name in script["deck"]]
    return new_state(len(hands), deck, mutations)


def play(name):
    return Action("play", Card(name))


def types(events):
    return [event["type"] for event in events]


def test_deals_round_robin_and_keeps_rest_as_draw_pile():
    state = make([["gear1", "gear2", "gear3", "vent"], ["jam", "scan", "double", "reverse"]], ["scan", "vent"])
    assert state.hands[0] == (Card.GEAR1, Card.GEAR2, Card.GEAR3, Card.VENT)
    assert state.hands[1] == (Card.JAM, Card.SCAN, Card.DOUBLE, Card.REVERSE)
    assert state.draw_pile == (Card.SCAN, Card.VENT)
    assert state.current == 0 and state.pressure == 0


def test_gear_raises_pressure_and_passes_turn():
    state = make([["gear3", "vent", "vent", "vent"], FILL])
    after, events = step(state, play("gear3"))
    assert after.pressure == 3
    assert after.current == 1
    assert {"type": "pressure", "value": 3} in events
    assert events[-1] == {"type": "turn_passed", "to": 1, "actions": 1, "turn": 2}


def test_step_never_mutates_the_input_state():
    state = make([["gear3", "vent", "vent", "vent"], FILL])
    snapshot = (state.hands, state.pressure, state.current, state.discard)
    step(state, play("gear3"))
    assert (state.hands, state.pressure, state.current, state.discard) == snapshot


@pytest.mark.parametrize(
    ("mutations", "start", "expected"),
    [((), 5, 2), ((), 2, 0), ((Mutation.VENT_FOUR,), 5, 1)],
)
def test_vent_cools_pressure_and_never_goes_negative(mutations, start, expected):
    state = replace(make([["vent", "vent", "vent", "vent"], FILL], mutations=mutations), pressure=start)
    after, _ = step(state, play("vent"))
    assert after.pressure == expected


@pytest.mark.parametrize(("mutations", "reset"), [((), 0), ((Mutation.HOT_RESTART,), 4)])
def test_overflow_eliminates_player_and_resets_pressure(mutations, reset):
    hands = [["gear2", "jam", "vent", "vent"], FILL, FILL]
    state = replace(make(hands, mutations=mutations), pressure=10)
    after, events = step(state, play("gear2"))
    assert after.alive == (False, True, True)
    assert after.hands[0] == ()
    assert after.pressure == reset
    assert after.current == 1
    assert types(events)[:4] == ["played", "pressure", "eliminated", "pressure"]


def test_double_gives_next_player_two_actions():
    state = make([["double", "vent", "vent", "vent"], FILL, FILL])
    after, events = step(state, play("double"))
    assert after.current == 1 and after.actions_left == 2
    assert events[-1]["actions"] == 2


@pytest.mark.parametrize(("mutations", "expected"), [((), 2), ((Mutation.STACKING_DOUBLE,), 3)])
def test_two_doubles_in_one_turn(mutations, expected):
    state = replace(make([["double", "double", "vent", "vent"], FILL, FILL], mutations=mutations), actions_left=2)
    middle, _ = step(state, play("double"))
    after, _ = step(middle, play("double"))
    assert after.current == 1 and after.actions_left == expected


@pytest.mark.parametrize(("mutations", "next_seat"), [((), 2), ((Mutation.REVERSE_SKIPS,), 1)])
def test_reverse_flips_turn_order(mutations, next_seat):
    state = make([["reverse", "vent", "vent", "vent"], FILL, FILL], mutations=mutations)
    after, _ = step(state, play("reverse"))
    assert after.current == next_seat


def test_jam_wastes_the_next_players_next_play_only():
    state = make([["jam", "vent", "vent", "vent"], ["gear3", "gear3", "vent", "vent"]])
    jammed, _ = step(state, play("jam"))
    wasted, events = step(jammed, play("gear3"))
    assert wasted.pressure == 0
    assert types(events) == ["played", "turn_passed"]
    back, _ = step(wasted, play("vent"))
    normal, _ = step(back, play("gear3"))
    assert normal.pressure == 3


@pytest.mark.parametrize(("mutations", "drew"), [((), True), ((Mutation.JAM_BLOCKS_DRAW,), False)])
def test_jam_and_drawing(mutations, drew):
    state = make([["jam", "vent", "vent", "vent"], FILL], pile=["scan"], mutations=mutations)
    jammed, _ = step(state, play("jam"))
    after, events = step(jammed, DRAW)
    assert (Card.SCAN in after.hands[1]) is drew
    assert ("drew" in types(events)) is drew


@pytest.mark.parametrize(("mutations", "seen"), [((), ["gear1", "gear2"]), ((Mutation.SHORT_SCAN,), ["gear1"])])
def test_scan_reveals_top_of_draw_pile(mutations, seen):
    state = make([["scan", "vent", "vent", "vent"], FILL], pile=["gear1", "gear2", "gear3"], mutations=mutations)
    _, events = step(state, play("scan"))
    assert {"type": "revealed", "seat": 0, "cards": seen} in events


def test_draw_takes_top_card_and_is_illegal_with_full_hand():
    state = make([["vent", "vent", "vent", "vent"], FILL], pile=["gear1", "gear2"])
    after, events = step(state, DRAW)
    assert after.hands[0][-1] == Card.GEAR1
    assert {"type": "drew", "seat": 0, "card": "gear1"} in events
    full = replace(state, hands=((Card.VENT,) * 6, state.hands[1]))
    assert DRAW not in legal_actions(full)


def test_pass_is_the_only_move_with_no_cards_and_empty_pile():
    state = make([FILL, FILL], pile=[])
    stuck = replace(state, hands=((), state.hands[1]), draw_pile=())
    assert legal_actions(stuck) == (PASS,)
    after, events = step(stuck, PASS)
    assert types(events) == ["passed", "turn_passed"]
    assert after.current == 1


def test_illegal_action_is_rejected():
    state = make([FILL, FILL])
    with pytest.raises(IllegalActionError):
        step(state, play("gear3"))


def test_game_ends_when_one_player_is_left():
    state = replace(make([["gear3", "vent", "vent", "vent"], FILL]), pressure=11)
    after, events = step(state, play("gear3"))
    assert after.over and after.winner == 1
    assert events[-1] == {"type": "game_over", "winner": 1}
    assert legal_actions(after) == ()


def test_rejects_bad_player_counts_and_short_decks():
    with pytest.raises(ValueError):
        new_state(1, [Card.VENT] * 10)
    with pytest.raises(ValueError):
        new_state(3, [Card.VENT] * 5)
