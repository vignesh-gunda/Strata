"""T5 tests: experiments, the runner, predict-then-check, designer and selector."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor.main.corpus import TraceStore, load_games, load_seed_corpus
from auditor.main.synthetic import make_game, make_view
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from llm.main.muse import MuseClient
from llm.main.subq import SubqClient
from prober.main.designer import design_experiments, select_experiment
from prober.main.experiments import (ActionSpec, Experiment, build_deck, seed_experiments,
                                     validate_experiment)
from prober.main.probe import experiments_done, hand_size_for, known_cards, main as probe_main
from prober.main.probe import run_seed_probes
from prober.main.runner import (GameRunner, PolicyChooser, ProbeBudget, ProbeBudgetExceeded,
                                RulePredictor, ScriptedChooser)
from sandbox.main.binary import BinaryProcess
from sandbox.main.digsite_fs import DigSiteFS

REAL = Path(__file__).resolve().parents[3] / "dig-site"
needs_binary = pytest.mark.skipif(
    not (REAL / "bin" / "pressure.pyz").exists()
    or not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists(),
    reason="dig-site not built")
CARDS = {"gear1", "gear2", "gear3", "vent", "reverse", "double", "jam", "scan"}


# ---------- experiments ----------

def test_build_deck_matches_round_robin_dealing():
    exp = Experiment(id="e", title="t", players=3, hands=[["a1", "a2"], ["b1", "b2"], ["c1", "c2"]],
                     pile=["p1", "p2"], policy="random")
    deck = build_deck(exp, 2)
    assert deck == ["a1", "b1", "c1", "a2", "b2", "c2", "p1", "p2"]
    for seat in range(3):
        assert deck[seat:2 * 3:3] == exp.hands[seat]
    assert deck[6:] == exp.pile


def test_validation_catches_bad_experiments():
    good = seed_experiments(4)[0]
    assert validate_experiment(good, 4, CARDS) == []
    wrong_size = good.model_copy(update={"hands": [["vent"] * 3, ["vent"] * 4]})
    assert any("must be 4" in e for e in validate_experiment(wrong_size, 4, CARDS))
    unknown = good.model_copy(update={"pile": ["laser"]})
    assert any("unknown cards" in e for e in validate_experiment(unknown, 4, CARDS))
    nothing = good.model_copy(update={"policy": None, "actions": []})
    assert any("actions or a policy" in e for e in validate_experiment(nothing, 4, CARDS))
    bad_action = good.model_copy(update={"actions": [ActionSpec(type="draw", card="vent")]})
    assert any("needs a card" in e for e in validate_experiment(bad_action, 4, CARDS))


def test_seed_experiments_are_valid_for_any_hand_size():
    for size in (3, 4, 5):
        experiments = seed_experiments(size)
        assert len({e.id for e in experiments}) == len(experiments) >= 10
        for exp in experiments:
            assert validate_experiment(exp, size, CARDS) == [], exp.id
            assert exp.claim_topics, exp.id


def test_seed_experiments_cover_the_lost_functions_and_parameters():
    topics = {t for e in seed_experiments() for t in e.claim_topics}
    for needed in ("function:_draw", "function:_double", "function:_reverse", "function:_gear",
                   "function:_vent", "function:_jam", "param:hand_limit", "param:scan_depth",
                   "ending:stalemate"):
        assert needed in topics


# ---------- choosers ----------

def test_scripted_chooser_deviates_safely_and_counts():
    chooser = ScriptedChooser([{"type": "play", "card": "reverse"}, {"type": "play", "card": "vent"}])
    legal = [{"type": "play", "card": "gear1"}, {"type": "play", "card": "scan"}, {"type": "draw"}]
    assert chooser({}, legal, 1) == {"type": "play", "card": "scan"}       # no gear fallback
    assert chooser({}, [{"type": "play", "card": "vent"}], 2) == {"type": "play", "card": "vent"}
    assert chooser({}, legal, 3) is None and chooser.deviations == 1


def test_policies():
    legal = [{"type": "play", "card": "gear2"}, {"type": "draw"}, {"type": "play", "card": "vent"}]
    assert PolicyChooser("draw_first")({}, legal, 1) == {"type": "draw"}
    assert PolicyChooser("draw_first")({}, legal[:1] + legal[2:], 1) == {"type": "play", "card": "vent"}
    assert PolicyChooser("random", 1)({}, legal, 1) in legal


# ---------- runner on a fake box ----------

class FakeBox:
    """A 3-step game. Records how many predictions were on file when each action ran."""

    def __init__(self, ledger=None, steps=3):
        self.ledger, self.steps, self.n = ledger, steps, 0
        self.seen_predictions = []

    def random_script(self, seed):
        return {"deck": ["vent"] * 42}

    def new_game(self, players, script):
        self.n = 0
        return {"game_id": "g-0001", "view": make_view(players)}

    def legal_actions(self, game_id):
        return [{"type": "play", "card": "vent"}, {"type": "draw"}]

    def act(self, game_id, action):
        self.seen_predictions.append(len(self.ledger.predictions()) if self.ledger else 0)
        self.n += 1
        over = self.n >= self.steps
        return {"events": [{"type": "played", "seat": 0, "card": "vent"}],
                "view": make_view(turn=self.n + 1, over=over, winner=0 if over else None)}


def runner_for(box=None, **kw):
    store = TraceStore([make_game("g-1", [{"action": {"type": "draw"}, "view": make_view()}])])
    ledger = Ledger(trace_validator=store.exists)
    box = box or FakeBox(ledger)
    return GameRunner(box, store, ledger=ledger, **kw), store, ledger, box


def test_runner_records_steps_with_x_ids_and_before_views():
    runner, store, _, _ = runner_for()
    game = runner.run_random(seed=1, players=2)
    assert [s["trace_id"] for s in game.steps] == ["x-000001", "x-000002", "x-000003"]
    assert game.game_id == "px-0001" and game.origin == "probe" and game.meta["binary_game_id"] == "g-0001"
    assert game.steps[0]["before"] == game.initial_view and game.steps[1]["before"] == game.steps[0]["view"]
    assert all(store.exists(s["trace_id"]) for s in game.steps)
    assert game.steps[-1]["view"]["over"]


def test_runner_saves_games_that_reload_identically(tmp_path):
    runner, _, _, _ = runner_for(save_path=tmp_path / "probe_games.jsonl")
    game = runner.run_random(seed=1, players=2)
    (loaded,) = load_games(tmp_path / "probe_games.jsonl")
    assert loaded.steps[2]["before"] == game.steps[2]["before"] and loaded.meta["seed"] == 1


def test_budget_stops_the_runner():
    runner, *_ = runner_for(budget=ProbeBudget(max_games=1))
    runner.run_random(1, 2)
    with pytest.raises(ProbeBudgetExceeded):
        runner.run_random(2, 2)
    capped, *_ = runner_for(budget=ProbeBudget(max_steps=2))
    with pytest.raises(ProbeBudgetExceeded):
        capped.run_random(1, 2)


def test_heldout_probe_games_stay_out_of_the_fitting_corpus():
    runner, store, _, _ = runner_for()
    train = runner.run_random(1, 2, split="train")
    held = runner.run_random(2, 2, split="heldout")
    fitting_ids = {g.game_id for g in store.fitting().games()}
    assert train.game_id in fitting_ids and held.game_id not in fitting_ids


# ---------- predict-then-check ----------

WRONG_LEGAL = ('KIND = "legal"\ndef predict(history, view):\n    return [{"type": "draw"}]\n')
RIGHT_LEGAL = ('KIND = "legal"\ndef predict(history, view):\n'
               '    return [{"type": "play", "card": "vent"}, {"type": "draw"}]\n')


def model_rule(ledger, code, status=Status.INFERRED):
    claim = ledger.add_claim(text="rule", source=Source.MODEL, created_by="t", key=code[:40] + str(status),
                             rule={"type": "executable", "group": "legal", "code": code})
    ledger.set_status(claim.id, status, ["t-000001"])
    return claim


def test_prediction_is_on_file_before_the_action_runs_and_surprise_is_recorded():
    runner, _, ledger, box = runner_for()
    wrong, right = model_rule(ledger, WRONG_LEGAL), model_rule(ledger, RIGHT_LEGAL)
    runner.predictor = RulePredictor(ledger)
    game = runner.run_random(1, 2)
    runner.predictor.close()
    assert box.seen_predictions == [1, 2, 3]            # logged first, one more each step
    predictions = ledger.predictions()
    assert [p.step for p in predictions] == [1, 2, 3]
    assert predictions[0].prediction["rules"][wrong.id] == [{"type": "draw"}]
    outcomes = [ledger.outcome(p.id) for p in predictions]
    assert all(o.surprise and o.actual["mismatched"] == [wrong.id] for o in outcomes)
    assert [s["surprise"] for s in game.steps] == [True, True, True]
    assert game.steps[0]["prediction"] and len(ledger.surprises()) == 3


def test_no_surprise_when_every_rule_is_right():
    runner, _, ledger, _ = runner_for()
    model_rule(ledger, RIGHT_LEGAL)
    runner.predictor = RulePredictor(ledger)
    game = runner.run_random(1, 2)
    runner.predictor.close()
    assert [s["surprise"] for s in game.steps] == [False] * 3 and not ledger.surprises()


def test_only_inferred_or_confirmed_rules_predict():
    runner, _, ledger, _ = runner_for()
    model_rule(ledger, RIGHT_LEGAL, status=Status.OBSERVED)
    predictor = RulePredictor(ledger)
    assert predictor.rule_ids == []
    predictor.close()


def test_unloadable_rules_are_skipped_not_fatal():
    _, _, ledger, _ = runner_for()
    model_rule(ledger, "import os\nKIND='legal'\ndef predict(h,v): return []")
    predictor = RulePredictor(ledger)
    assert predictor.rule_ids == []


# ---------- against the real binary ----------

@pytest.fixture(scope="module")
def real_games():
    if not (REAL / "bin" / "pressure.pyz").exists() \
            or not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists():
        pytest.skip("dig-site not built")
    fs = DigSiteFS(REAL)
    store = TraceStore(load_seed_corpus(fs))
    with BinaryProcess(fs) as box:
        runner = GameRunner(box, store)
        games = {g.meta["experiment_id"]: g for g in run_seed_probes(runner, 4)}
    return games, store, runner


@needs_binary
def test_every_seed_experiment_runs_and_deals_the_stacked_hands(real_games):
    games, _, _ = real_games
    assert set(games) == {e.id for e in seed_experiments(4)}
    by_id = {e.id: e for e in seed_experiments(4)}
    for exp_id, game in games.items():
        assert game.initial_view["hands"] == by_id[exp_id].hands, exp_id
        assert game.steps


@needs_binary
def test_scripted_experiments_followed_their_script(real_games):
    games, _, _ = real_games
    clean = ["e-jam-draw", "e-double-twice", "e-reverse-3p", "e-gear-arith", "e-overflow-2p",
             "e-vent-floor", "e-stalemate", "e-scan-short"]
    assert [games[i].meta["deviations"] for i in clean] == [0] * len(clean)


@needs_binary
def test_experiments_reach_the_endings_they_target(real_games):
    games, _, _ = real_games
    stalemate = games["e-stalemate"].steps[-1]
    assert stalemate["view"]["over"] and stalemate["view"]["winner"] is None
    assert stalemate["view"]["draw_pile_size"] == 0
    overflow = games["e-overflow-2p"].steps[-1]
    assert overflow["view"]["winner"] == 1 and overflow["events"][-1]["type"] == "game_over"
    assert any(e["type"] == "eliminated" for s in games["e-overflow-3p"].steps for e in s["events"])


@needs_binary
def test_draw_limit_experiment_finds_the_hand_limit(real_games):
    games, _, _ = real_games
    steps = games["e-draw-limit"].steps
    sizes = [len(s["before"]["hands"][s["seat"]]) for s in steps]
    assert max(sizes) == 6
    assert all(not any(a["type"] == "draw" for a in s["legal"])
               for s in steps if len(s["before"]["hands"][s["seat"]]) == 6
               and s["before"]["draw_pile_size"] > 0)


@needs_binary
def test_probe_traces_are_citable_and_new(real_games):
    games, store, _ = real_games
    ids = [s["trace_id"] for g in games.values() for s in g.steps]
    assert len(ids) == len(set(ids)) and all(i.startswith("x-") and store.exists(i) for i in ids)
    assert experiments_done(store) == set(games)


@needs_binary
def test_probe_cli_is_resumable(tmp_path):
    args = ["--runs-dir", str(tmp_path), "--run-id", "r-p", "--only", "e-stalemate,e-scan-short"]
    assert probe_main(args) == 0
    first = load_games(tmp_path / "r-p" / "probe_games.jsonl")
    assert sorted(g.meta["experiment_id"] for g in first) == ["e-scan-short", "e-stalemate"]
    assert probe_main(args) == 0                                  # nothing new to run
    assert len(load_games(tmp_path / "r-p" / "probe_games.jsonl")) == 2
    ledger_free = TraceStore(load_seed_corpus(DigSiteFS(REAL)))
    assert hand_size_for(Ledger(), ledger_free) == 4
    assert known_cards(ledger_free) == CARDS


# ---------- designer and selector ----------

class FakeMuse:
    def __init__(self, text):
        self.text, self.requests = text, []

    def __call__(self, url, headers, body, timeout):
        self.requests.append(body)
        return 200, {"output": [{"type": "message", "content": [{"type": "output_text",
                                                                "text": self.text}]}],
                     "usage": {"total_tokens": 7}}


def exp_json(exp_id, hand=4, cards=("vent",)):
    return {"id": exp_id, "title": "t", "players": 2, "hands": [[cards[0]] * hand] * 2,
            "pile": ["vent"] * 4, "actions": [{"type": "play", "card": "vent"}],
            "claim_topics": ["function:_jam"], "rationale": "r"}


def test_designer_keeps_valid_experiments_and_reports_why_others_failed():
    reply = json.dumps({"experiments": [exp_json("e-m1"), exp_json("e-m2", hand=3),
                                        exp_json("e-m3", cards=("laser",)), exp_json("e-draw-limit")]})
    transport = FakeMuse(reply)
    muse = MuseClient("k", transport=transport, sleep=lambda s: None)
    valid, rejected = design_experiments(
        muse, hand_size=4, known_cards=CARDS,
        hypotheses=[{"id": "c-1", "text": "jam clears on draw", "code": "KIND='legal'"}],
        sample_steps=[], existing_ids={"e-draw-limit"}, count=4)
    assert [e.id for e in valid] == ["e-m1"]
    assert len(rejected) == 3
    assert any("must be 4" in r for r in rejected) and any("unknown cards" in r for r in rejected)
    assert any("already used" in r for r in rejected)
    assert "services" not in transport.requests[0]["input"]


class FakeSubq:
    def __init__(self, pick=None, fail=False):
        self.pick, self.fail, self.calls = pick, fail, []

    def choice(self, state, instructions, options):
        from llm.main.subq import Choice, SubqError
        self.calls.append((state, options))
        if self.fail:
            raise SubqError(503, "unavailable", "down")
        return Choice(self.pick, {k: 0.0 for k in options}, 0.9)


def test_selector_uses_subq_then_falls_back_to_probe_order():
    experiments = seed_experiments(4)
    probe_list = [{"topic": "function:_double", "rank": 1, "text": "double lost"},
                  {"topic": "function:_draw", "rank": 2, "text": "draw lost"}]
    ledger = Ledger()
    subq = FakeSubq(pick="e-stalemate")
    assert select_experiment(subq, ledger, experiments, probe_list).id == "e-stalemate"
    assert set(subq.calls[0][1]) == {e.id for e in experiments}
    assert select_experiment(FakeSubq(fail=True), ledger, experiments, probe_list).id == "e-double-twice"
    assert select_experiment(None, ledger, experiments, probe_list).id == "e-double-twice"
    assert select_experiment(subq, ledger, experiments[:1], probe_list).id == experiments[0].id
