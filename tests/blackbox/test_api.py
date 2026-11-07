"""Tests for the session API, scripts, mutations and opaque-token mode."""

import pytest

from blackbox.api import BlackBox, GameNotFoundError
from blackbox.cards import Card, standard_deck
from blackbox.engine import IllegalActionError
from blackbox.rules import Mutation, pick_mutations
from blackbox.scripts import script_from_seed, stacked_script

REAL_NAMES = {card.value for card in Card}


def play_first_legal(box, game_id, steps):
    log = []
    for _ in range(steps):
        actions = box.legal_actions(game_id)
        if not actions:
            break
        log.append(box.act(game_id, actions[0]))
    return log


def test_new_game_returns_id_and_view_without_hidden_state():
    box = BlackBox()
    game = box.new_game(3, script_from_seed(7))
    assert game["game_id"] == "g-0001"
    view = game["view"]
    assert view["players"] == 3 and view["current_seat"] == 0
    assert view["draw_pile_size"] == len(standard_deck()) - 12
    hidden = {"direction", "jammed", "draw_pile", "rules", "extra_actions", "pressure_limit"}
    assert hidden.isdisjoint(view)


def test_same_script_gives_identical_games():
    first, second = BlackBox(), BlackBox()
    a = first.new_game(2, script_from_seed(3))["game_id"]
    b = second.new_game(2, script_from_seed(3))["game_id"]
    assert play_first_legal(first, a, 40) == play_first_legal(second, b, 40)


def test_script_from_seed_is_a_permutation_of_the_standard_deck():
    deck = script_from_seed(11)["deck"]
    assert sorted(deck) == sorted(card.value for card in standard_deck())
    assert deck != script_from_seed(12)["deck"]


def test_stacked_script_validates_hands():
    with pytest.raises(ValueError):
        stacked_script([["vent"] * 4, ["vent"] * 3], [])
    with pytest.raises(ValueError):
        stacked_script([["vent"] * 4, ["bomb", "vent", "vent", "vent"]], [])


def test_rejects_unknown_cards_bad_actions_and_games():
    box = BlackBox()
    with pytest.raises(ValueError):
        box.new_game(2, {"deck": ["kitten"] * 20})
    with pytest.raises(ValueError):
        box.new_game(9, script_from_seed(1))
    game_id = box.new_game(2, script_from_seed(1))["game_id"]
    with pytest.raises(ValueError):
        box.act(game_id, {"type": "explode"})
    with pytest.raises(GameNotFoundError):
        box.view("g-9999")


def test_illegal_but_well_formed_action_raises():
    box = BlackBox()
    hands = [["vent"] * 4, ["vent"] * 4]
    game_id = box.new_game(2, stacked_script(hands, ["vent"]))["game_id"]
    with pytest.raises(IllegalActionError):
        box.act(game_id, {"type": "play", "card": "gear3"})


def test_pick_mutations_is_deterministic_and_bounded():
    assert pick_mutations(42, 2) == pick_mutations(42, 2)
    assert len(set(pick_mutations(5, 3))) == 3
    with pytest.raises(ValueError):
        pick_mutations(1, 99)


def test_mutations_are_hidden_until_reveal():
    box = BlackBox(mutations=(Mutation.VENT_FOUR,))
    game = box.new_game(2, script_from_seed(2))
    assert "vent_four" not in str(game)
    assert box.reveal()["mutations"] == ["vent_four"]


def test_opaque_mode_hides_real_card_and_action_names():
    box = BlackBox(opaque_seed=9)
    script = box.random_script(4)
    assert REAL_NAMES.isdisjoint(script["deck"])
    game = box.new_game(2, script)
    names = {name for hand in game["view"]["hands"] for name in hand}
    assert names and names.isdisjoint(REAL_NAMES)
    actions = box.legal_actions(game["game_id"])
    assert all(action["type"].startswith("a") for action in actions)
    result = box.act(game["game_id"], actions[0])
    assert REAL_NAMES.isdisjoint(str(result).split("'"))
    assert all(event["type"].startswith("e") for event in result["events"])
    assert set(box.reveal()["cards"]) == REAL_NAMES


def test_opaque_errors_do_not_leak_real_names():
    box = BlackBox(opaque_seed=9)
    game_id = box.new_game(2, box.random_script(4))["game_id"]
    hand = set(box.view(game_id)["hands"][0])
    missing = next(token for token in box.reveal()["cards"].values() if token not in hand)
    play = next(action["type"] for action in box.legal_actions(game_id) if "card" in action)
    for bad in ({"type": play, "card": missing}, {"type": play}):
        with pytest.raises(ValueError) as error:
            box.act(game_id, bad)
        assert not any(name in str(error.value) for name in REAL_NAMES | {"play", "draw", "pass"})
