"""T8 tests: the gate accepts a faithful rebuild and blocks a broken one with a short counterexample."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

from auditor.main.corpus import GameRecord, load_seed_corpus
from auditor.main.synthetic import make_game, make_view
from sandbox.main.binary import BinaryProcess
from sandbox.main.digsite_fs import DigSiteFS
from verifier.main.checks import fuzz, replay_traces, shrink, verify
from verifier.main.cli import main as verifier_main
from verifier.main.driver import FuzzChooser, ListChooser, SideBySide

HERE = Path(__file__).parent
TOY = str(HERE / "toy_game.py")
PROXY = str(HERE / "mutant_proxy.py")
REAL = HERE.parents[2] / "dig-site"
BINARY = REAL / "bin" / "pressure.pyz"
needs_binary = pytest.mark.skipif(not BINARY.exists(), reason="dig-site not built")
needs_corpus = pytest.mark.skipif(
    not BINARY.exists() or not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists(),
    reason="dig-site not built")


def toy(*flags):
    return BinaryProcess(command=[sys.executable, TOY, *flags], timeout=10)


DRAW, VENT = {"type": "draw"}, {"type": "play", "card": "vent"}


def recorded_toy_game(actions):
    """Record a game from the toy so replay has something real to compare with."""
    with toy() as box:
        script = box.random_script(1)
        started = box.new_game(2, script)
        steps, view = [], started["view"]
        for n, action in enumerate(actions, 1):
            legal = box.legal_actions(started["game_id"])
            reply = box.act(started["game_id"], action)
            steps.append({"trace_id": f"t-{n:06d}", "step": n, "seat": 0, "turn": n,
                          "action": action, "legal": legal, "events": reply["events"],
                          "view": reply["view"]})
            view = reply["view"]
    game = GameRecord("g-1", 2, script, started["view"], "train", "toy", steps)
    previous = started["view"]
    for s in steps:
        s["before"], previous = previous, s["view"]
    return game


# ---------- the gate on a toy game ----------

def test_identical_programs_are_accepted():
    with toy() as ref, toy() as cand:
        verdict = verify(ref, cand, [], fuzz_games=150, min_fuzz_games=150, seed=3)
    assert verdict.verdict == "Accepted" and verdict.fuzz.games == 150
    assert "0 divergences" in verdict.reason and verdict.counterexample is None


def test_a_rare_bug_is_found_and_shrunk_to_the_shortest_game():
    with toy() as ref, toy("--bug") as cand:
        verdict = verify(ref, cand, [], fuzz_games=400, min_fuzz_games=400, seed=3)
    assert verdict.verdict == "Blocked"
    ce = verdict.counterexample
    assert ce["source"] == "fuzz" and ce["kind"] == "act"
    assert ce["actions"] == [DRAW, DRAW, DRAW, VENT]            # the smallest game that exposes it
    assert ce["shrunk_from"] >= len(ce["actions"])
    assert ce["reference"]["view"]["pressure"] == 2 and ce["candidate"]["view"]["pressure"] == 1
    assert ce["reference_trace"][-1]["action"] == VENT          # shows what the original did
    assert "shortest game has 4 action(s)" in verdict.reason


def test_shrink_result_still_diverges_and_is_locally_minimal():
    with toy() as ref, toy("--bug") as cand:
        pair = SideBySide(ref, cand)
        script = {"deck": ["vent"] * 42}
        long_actions = [DRAW, VENT, DRAW, DRAW, DRAW, VENT, DRAW]       # reaches counter 3, then vent
        outcome = pair.play(2, script, ListChooser(long_actions))
        assert outcome.divergence is not None
        actions, final = shrink(pair, 2, script, long_actions, outcome.divergence)
        assert len(actions) == 4 and final is not None
        for i in range(len(actions)):                            # removing any action loses the bug
            reduced = actions[:i] + actions[i + 1:]
            assert pair.play(2, script, ListChooser(reduced)).divergence is None


def test_fuzz_must_actually_run_enough_games():
    with toy() as ref, toy() as cand:
        verdict = verify(ref, cand, [], fuzz_games=10, min_fuzz_games=100)
    assert verdict.verdict == "Blocked" and "only 10 fuzzed games" in verdict.reason


def test_a_crashing_candidate_is_blocked_and_the_run_survives():
    with toy() as ref, toy("--crash") as cand:
        verdict = verify(ref, cand, [], fuzz_games=300, min_fuzz_games=300, seed=1)
    assert verdict.verdict == "Blocked" and verdict.counterexample["kind"] == "crash"


def test_wrong_error_handling_is_a_divergence():
    with toy() as ref, toy("--lenient") as cand:
        pair = SideBySide(ref, cand)
        result = fuzz(pair, games=60, seed=2, illegal_rate=0.5)
    assert result.divergence is not None and result.divergence.kind == "illegal"
    with toy() as ref, toy() as cand:                            # same errors on both: fine
        assert fuzz(SideBySide(ref, cand), games=60, seed=2, illegal_rate=0.5).divergence is None


def test_fuzz_is_deterministic_for_a_seed():
    def run():
        with toy() as ref, toy("--bug") as cand:
            r = fuzz(SideBySide(ref, cand), games=200, seed=9)
            return r.games, r.divergence.actions if r.divergence else None
    assert run() == run()


def test_extra_scripts_are_played_first():
    with toy() as ref, toy() as cand:
        seen = []
        original = ref.new_game
        ref.new_game = lambda players, script: (seen.append((players, script)), original(players, script))[1]
        fuzz(SideBySide(ref, cand), games=3, extra_scripts=[(3, {"deck": ["vent"] * 42})])
    assert seen[0] == (3, {"deck": ["vent"] * 42})


def test_fuzz_choosers_have_four_distinct_modes():
    legal = [{"type": "draw"}, {"type": "play", "card": "gear1"}, {"type": "play", "card": "vent"}]
    picks = {mode: [FuzzChooser(random.Random(5), mode)({}, legal, 1) for _ in range(20)]
             for mode in range(4)}
    assert all(a == DRAW for a in picks[1])                       # draw-heavy
    assert all(a != {"type": "play", "card": "gear1"} for a in picks[2])   # calm
    assert all(a["type"] == "play" for a in picks[3])             # plays


# ---------- replay ----------

def test_replay_accepts_a_faithful_candidate_and_names_the_tampered_step():
    game = recorded_toy_game([DRAW, DRAW, VENT])
    with toy() as cand:
        ok = replay_traces(cand, [game])
    assert ok.games == 1 and ok.steps == 3 and not ok.divergences
    game.steps[1]["events"][0]["value"] = 99
    with toy() as cand:
        bad = replay_traces(cand, [game])
    assert bad.divergences[0]["what"] == "events" and bad.divergences[0]["trace_id"] == "t-000002"
    game.steps[1]["events"][0]["value"] = 2
    game.initial_view = make_view()
    with toy() as cand:
        assert replay_traces(cand, [game]).divergences[0]["what"] == "initial view"


def test_gate_blocks_on_replay_before_fuzzing():
    game = recorded_toy_game([DRAW, DRAW])
    game.steps[0]["view"]["pressure"] = 42
    with toy() as ref, toy() as cand:
        verdict = verify(ref, cand, [game], fuzz_games=50, min_fuzz_games=50)
    assert verdict.verdict == "Blocked" and verdict.counterexample["source"] == "replay"
    assert verdict.fuzz.games == 0 and "t-000001" in json.dumps(verdict.counterexample)


# ---------- against the real binary ----------

@needs_corpus
def test_the_original_replays_its_own_recorded_traces():
    fs = DigSiteFS(REAL)
    recorded = load_seed_corpus(fs)
    with BinaryProcess(fs) as cand:
        result = replay_traces(cand, recorded)
    assert result.games == 40 and result.steps == 2243 and not result.divergences


@needs_corpus
def test_binary_vs_faithful_copy_is_accepted_with_recorded_decks_first():
    fs = DigSiteFS(REAL)
    recorded = load_seed_corpus(fs)
    extra = [(g.players, g.script) for g in recorded]
    with BinaryProcess(fs) as ref, \
            BinaryProcess(command=[sys.executable, PROXY, str(BINARY), "--identity"], timeout=20) as cand:
        verdict = verify(ref, cand, recorded, fuzz_games=120, min_fuzz_games=120, extra_scripts=extra)
    assert verdict.verdict == "Accepted", verdict.reason
    assert verdict.replay.steps == 2243 and verdict.fuzz.games == 120


@needs_corpus
def test_binary_vs_off_by_one_vent_is_blocked_with_a_tiny_counterexample():
    fs = DigSiteFS(REAL)
    recorded = load_seed_corpus(fs)
    with BinaryProcess(fs) as ref, \
            BinaryProcess(command=[sys.executable, PROXY, str(BINARY)], timeout=20) as cand:
        verdict = verify(ref, cand, recorded, fuzz_games=50, min_fuzz_games=50)
    assert verdict.verdict == "Blocked"
    assert verdict.counterexample["source"] == "replay"          # recorded traces catch it first
    with BinaryProcess(fs) as ref, \
            BinaryProcess(command=[sys.executable, PROXY, str(BINARY)], timeout=20) as cand:
        verdict = verify(ref, cand, [], fuzz_games=300, min_fuzz_games=300)   # fuzz alone also does
    ce = verdict.counterexample
    assert verdict.verdict == "Blocked" and ce["source"] == "fuzz" and 1 <= ce["steps"] <= 3
    assert ce["actions"][-1]["card"] == "vent"
    assert ce["reference"]["events"] != ce["candidate"]["events"]


@needs_corpus
def test_cli_exit_code_and_verdict_file(tmp_path):
    good = [sys.executable, PROXY, str(BINARY), "--identity"]
    code = verifier_main(["--candidate", " ".join(good), "--runs-dir", str(tmp_path), "--run-id", "r-v",
                          "--fuzz", "5"])
    assert code == 1                                            # only 5 fuzz games < the 5000 gate
    verdict = json.loads((tmp_path / "r-v" / "verdict.json").read_text())
    assert verdict["verdict"] == "Blocked" and "only 5" in verdict["reason"]
    bad = [sys.executable, PROXY, str(BINARY)]
    assert verifier_main(["--candidate", " ".join(bad), "--runs-dir", str(tmp_path),
                          "--run-id", "r-v2", "--fuzz", "5"]) == 1
    assert json.loads((tmp_path / "r-v2" / "verdict.json").read_text())["counterexample"]


# ---------- independence (T8.6) ----------

def test_verifier_uses_no_model_and_reads_no_manual_or_partial_code():
    import re
    forbidden = [r"\bllm\b", r"surveyor", r"MuseClient", r"SubqClient", r"pressure-manual",
                 r"source/engine", r"docs/", r"hypothesizer", r"rebuilder", r"services/"]
    for path in (HERE.parent / "main").glob("*.py"):
        text = path.read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for pattern in forbidden:
            assert not re.search(pattern, code), f"{path.name} mentions {pattern}"
