"""End-to-end tests: the black box runs as a separate process."""

import pytest

from blackbox.client import BlackBoxClient, BlackBoxError
from blackbox.scripts import script_from_seed, stacked_script


def test_client_drives_a_game_in_another_process():
    with BlackBoxClient(seed=1, mutations=1) as box:
        game = box.new_game(2, script_from_seed(5))
        game_id = game["game_id"]
        actions = box.legal_actions(game_id)
        result = box.act(game_id, actions[0])
        assert result["events"][0]["seat"] == 0
        assert box.view(game_id)["turn"] >= 1


def test_errors_come_back_as_typed_exceptions():
    with BlackBoxClient(seed=1) as box:
        hands = [["vent"] * 4, ["vent"] * 4]
        game_id = box.new_game(2, stacked_script(hands, ["vent"]))["game_id"]
        with pytest.raises(BlackBoxError) as illegal:
            box.act(game_id, {"type": "play", "card": "gear3"})
        assert illegal.value.kind == "illegal_action"
        with pytest.raises(BlackBoxError) as missing:
            box.view("g-0404")
        assert missing.value.kind == "game_not_found"


def test_opaque_server_gives_scripts_in_visible_names():
    with BlackBoxClient(seed=8, opaque=True) as box:
        script = box.random_script(2)
        assert all(name.startswith("c") for name in script["deck"])
        game = box.new_game(3, script)
        assert box.legal_actions(game["game_id"])


def test_hostile_input_does_not_kill_the_server():
    from blackbox.api import BlackBox
    from blackbox.server import handle_line

    box = BlackBox()
    assert handle_line(box, "[" * 100_000)["error"]["kind"] == "invalid_request"
    assert handle_line(box, '{"method": "act", "params": {"game_id": 5, "action": []}}')["ok"] is False
    assert handle_line(box, '{"method": "new_game", "params": {"players": 2, "script": {"deck": []}}}')["ok"] is False


def test_explicit_mutations_and_reveal():
    with BlackBoxClient(mutation_names=("short_scan",), allow_reveal=True) as box:
        assert box.reveal()["mutations"] == ["short_scan"]


def test_reveal_is_blocked_by_default():
    with BlackBoxClient(seed=3, mutations=2) as box:
        with pytest.raises(BlackBoxError) as blocked:
            box.reveal()
        assert blocked.value.kind == "forbidden"
