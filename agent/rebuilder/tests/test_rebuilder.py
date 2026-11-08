"""T7 tests: the rebuild pipeline, with Muse scripted and a toy game standing in for Pressure."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from auditor.main.synthetic import make_game, make_view
from auditor.main.corpus import TraceStore
from llm.main.muse import MuseClient
from rebuilder.main.rebuild import (RebuildError, Rebuilder, build_and_repair, extract_js, node_check,
                                    smoke_game, write_rebuild)
from sandbox.main.binary import BinaryProcess
from verifier.main.checks import verify

HERE = Path(__file__).parent
TOY_ENGINE = (HERE / "fixtures" / "toy_engine.js").read_text()
TOY_PY = str(HERE.parent.parent / "verifier" / "tests" / "toy_game.py")

GOOD = TOY_ENGINE
BUGGY = TOY_ENGINE.replace("process.env.TOY_BUG === '1'", "true")
UNKNOWN = TOY_ENGINE.replace("process.env.TOY_UNKNOWN === '1'", "true")

RULES = [
    {"claim_id": "c-1", "group": "pressure", "kind": "events", "covers": ["pressure"],
     "text": "[pressure] draw adds one, vent removes one", "evidence": ["t-000001", "x-000002"],
     "code": "KIND = 'events'\nCOVERS = ['pressure']\ndef predict(history, view, action, prior_events):\n    return None\n",
     "experiments": []},
]


def fenced(js):
    return "Here is the engine:\n```javascript\n" + js + "\n```\n"


class ScriptedMuse:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.prompts.append(body["input"])
        text = self.replies.pop(0)
        return 200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                     "usage": {"total_tokens": 5}}


def world(*replies):
    store = TraceStore([make_game("g-1", [{"action": {"type": "draw"}, "view": make_view(pressure=1),
                                           "events": [{"type": "pressure", "value": 1}]}]),
                        make_game("g-2", [{"action": {"type": "draw"}, "view": make_view()}],
                                  split="heldout", start=50)])
    transport = ScriptedMuse(*replies)
    muse = MuseClient("k", transport=transport, sleep=lambda s: None)
    return Rebuilder(muse, store, lead_code="# recovered _advance_turn"), store, transport


def verify_against_toy(command):
    with BinaryProcess(command=[sys.executable, TOY_PY], timeout=10) as ref, \
            BinaryProcess(command=command, timeout=10) as cand:
        return verify(ref, cand, [], fuzz_games=200, min_fuzz_games=200, seed=3)


# ---------- extraction and checks ----------

def test_extract_js_prefers_the_longest_fenced_block_and_requires_exports():
    reply = "```text\nignore\n```\n```javascript\nexports.a = 1;\nexports.b = 2;\n```"
    assert extract_js(reply).startswith("exports.a")
    assert extract_js("module.exports = {};") == "module.exports = {};\n"
    with pytest.raises(RebuildError, match="module.exports"):
        extract_js("```javascript\nconsole.log(1)\n```")


def test_node_check_flags_syntax_errors(tmp_path):
    good, bad = tmp_path / "good.js", tmp_path / "bad.js"
    good.write_text("exports.x = 1;\n")
    bad.write_text("exports.x = ;\n")
    assert node_check(good) is None
    assert "SyntaxError" in node_check(bad)


# ---------- the fixed server around an engine ----------

def test_server_wraps_an_engine_in_the_same_contract_as_the_original(tmp_path):
    write_rebuild(tmp_path, GOOD)
    command = ["node", str(tmp_path / "server.js")]
    assert smoke_game(command, ["vent"] * 42) is None
    verdict = verify_against_toy(command)
    assert verdict.verdict == "Accepted", verdict.reason


def test_server_error_kinds(tmp_path):
    from sandbox.main.binary import BinaryRequestError
    write_rebuild(tmp_path, GOOD)
    with BinaryProcess(command=["node", str(tmp_path / "server.js")], timeout=10) as box:
        for bad_players in (1, 5, "2"):
            with pytest.raises(BinaryRequestError) as info:
                box.new_game(bad_players, {"deck": []})
            assert info.value.kind == "invalid_request"
        with pytest.raises(BinaryRequestError) as info:
            box.view("g-9999")
        assert info.value.kind == "game_not_found"
        gid = box.new_game(2, {"deck": ["vent"] * 8})["game_id"]
        assert gid == "g-0001"
        with pytest.raises(BinaryRequestError) as info:
            box.act(gid, {"type": "play", "card": "gear3"})
        assert info.value.kind == "illegal_action"
        with pytest.raises(BinaryRequestError) as info:
            box.act(gid, {"type": 5})
        assert info.value.kind == "invalid_request"
        assert len(box.random_script(3)["deck"]) == 42          # from DECK_COUNTS, deterministic
        assert box.random_script(3) == box.random_script(3)


def test_a_planted_bug_in_the_js_is_blocked_with_the_shortest_game(tmp_path):
    write_rebuild(tmp_path, BUGGY)
    verdict = verify_against_toy(["node", str(tmp_path / "server.js")])
    assert verdict.verdict == "Blocked" and verdict.counterexample["steps"] == 4


def test_explicit_unknown_behavior_surfaces_as_a_counterexample_with_its_message(tmp_path):
    write_rebuild(tmp_path, UNKNOWN)
    verdict = verify_against_toy(["node", str(tmp_path / "server.js")])
    ce = verdict.counterexample
    assert verdict.verdict == "Blocked" and ce["candidate"] == "internal_error"
    assert "unknown behavior: vent at 3" in ce["candidate_error"]


def test_smoke_game_catches_broken_engines(tmp_path):
    broken = GOOD.replace("exports.view = (s) => ({", "exports.view = (s) => ({ __x: 1,").replace(
        "players: s.players,", "")
    write_rebuild(tmp_path, broken)
    assert "lacks 'players'" in smoke_game(["node", str(tmp_path / "server.js")], ["vent"] * 42)
    crashing = GOOD.replace("exports.act = (s, action) => {", "exports.act = (s, action) => { throw new Error('x');")
    write_rebuild(tmp_path, crashing)
    assert smoke_game(["node", str(tmp_path / "server.js")], ["vent"] * 42) is not None


# ---------- building with Muse ----------

def test_build_prompt_states_rules_missing_groups_and_facts_but_no_forbidden_data(tmp_path):
    rebuilder, store, _ = world()
    prompt = rebuilder.build_prompt(RULES)
    assert "### [pressure]" in prompt and "evidence: t-000001, x-000002" in prompt
    assert "GROUPS WITH NO CONFIRMED RULE" in prompt and "turn_passed" in prompt
    assert "[pressure]" not in prompt.split("GROUPS WITH NO CONFIRMED RULE")[1].split("\n")[0]
    assert "a full deck holds 42 cards" in prompt and "_advance_turn" in prompt
    assert "sorted by action type then by card" not in prompt or "SORTED" in prompt
    assert "t-000050" not in prompt                          # nothing from the held-out game
    assert "services/" not in prompt and "sealed" not in prompt


def test_build_writes_files_checks_syntax_and_smoke(tmp_path):
    rebuilder, _, transport = world(fenced(UNKNOWN))
    result = rebuilder.build(RULES, tmp_path / "rb")
    assert result.ready and result.syntax_ok and result.smoke_ok
    assert result.unknown_behaviors == ["what vent does at pressure 3"]
    assert (tmp_path / "rb" / "engine.js").exists() and (tmp_path / "rb" / "server.js").exists()
    assert result.command == ["node", str(tmp_path / "rb" / "server.js")]


def test_build_refuses_to_run_without_confirmed_rules(tmp_path):
    rebuilder, _, transport = world()
    with pytest.raises(RebuildError, match="no Confirmed rules"):
        rebuilder.build([], tmp_path)
    assert transport.prompts == []                           # no model call was made


def test_a_syntax_error_reply_is_reported_not_run(tmp_path):
    rebuilder, _, _ = world(fenced("exports.x = ;"))
    result = rebuilder.build(RULES, tmp_path / "rb")
    assert not result.ready and result.problems[0].startswith("syntax:")


# ---------- the repair loop ----------

def test_repair_loop_fixes_the_bug_from_the_counterexample(tmp_path):
    rebuilder, _, transport = world(fenced(BUGGY), fenced(GOOD))
    seen = []
    outcome = build_and_repair(rebuilder, RULES, tmp_path / "rb", verify_against_toy,
                               max_attempts=3, on_counterexample=seen.append)
    assert outcome.accepted and outcome.attempts == 2
    assert [h["verdict"] for h in outcome.history] == ["Blocked", "Accepted"]
    assert len(seen) == 1 and seen[0]["actions"] == [{"type": "draw"}] * 3 + [{"type": "play", "card": "vent"}]
    repair_prompt = transport.prompts[1]
    assert "COUNTEREXAMPLE (fuzz)" in repair_prompt and "What the ORIGINAL did" in repair_prompt
    assert BUGGY.strip()[:40] in repair_prompt                # shows its own failing code
    assert "services/" not in repair_prompt
    assert (tmp_path / "rb" / "engine.js").read_text().strip() == GOOD.strip()


def test_repair_loop_gives_up_honestly(tmp_path):
    rebuilder, _, transport = world(fenced(BUGGY), fenced(BUGGY), fenced(BUGGY))
    outcome = build_and_repair(rebuilder, RULES, tmp_path / "rb", verify_against_toy, max_attempts=3)
    assert not outcome.accepted and outcome.attempts == 3 and len(outcome.history) == 3
    assert len(transport.prompts) == 3                       # 1 build + 2 repairs, none after the cap


def test_repair_loop_recovers_from_code_that_does_not_run(tmp_path):
    rebuilder, _, transport = world(fenced("exports.x = ;"), fenced(GOOD))
    outcome = build_and_repair(rebuilder, RULES, tmp_path / "rb", verify_against_toy, max_attempts=3)
    assert outcome.accepted and outcome.history[0]["verdict"] == "not runnable"
    assert "syntax" in transport.prompts[1]


def test_unknown_behaviors_are_reported_in_the_history(tmp_path):
    rebuilder, _, _ = world(fenced(UNKNOWN), fenced(GOOD))
    outcome = build_and_repair(rebuilder, RULES, tmp_path / "rb", verify_against_toy, max_attempts=3)
    assert outcome.history[0]["unknown_behaviors"] == ["what vent does at pressure 3"]
    assert outcome.accepted
