"""T4 tests: corpus split, checkers, and the audit of survey claims."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from auditor.main.audit import main as audit_main
from auditor.main.audit import run_audit
from auditor.main.checkers import (CHECKERS, check_hand_limit, check_jam, check_pressure_delta,
                                   check_reverse, measure_hand_limit)
from auditor.main.corpus import (FittingCorpus, GameRecord, TraceStore, load_games,
                                 load_seed_corpus, save_games)
from auditor.main.synthetic import make_game, make_view
from ledger.main.models import FindingKind, Source, Status, Tier
from ledger.main.store import Ledger
from llm.main.subq import SubqClient
from sandbox.main.digsite_fs import DigSiteFS
from scorer.main.scorer import Scorer
from surveyor.main.survey import main as survey_main
from surveyor.main.survey import run_survey

REAL = Path(__file__).resolve().parents[3] / "dig-site"
needs_corpus = pytest.mark.skipif(not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists(),
                                  reason="seed corpus not built")
PLAY = lambda card: {"type": "play", "card": card}


def fit(*games):
    return FittingCorpus(list(games))


def pressure_step(card, before, after, extra=()):
    events = [{"type": "played", "seat": 0, "card": card},
              {"type": "pressure", "value": after}, *extra]
    return {"action": PLAY(card), "events": events, "view": make_view(pressure=after)}


# ---------- corpus ----------

@needs_corpus
def test_seed_corpus_loads_with_before_views():
    games = load_seed_corpus(DigSiteFS(REAL))
    assert len(games) == 40 and sum(len(g.steps) for g in games) == 2243
    game = games[0]
    assert game.steps[0]["before"] == game.initial_view
    assert game.steps[1]["before"] == game.steps[0]["view"]


@needs_corpus
def test_fitting_corpus_has_no_heldout_steps_at_all():
    store = TraceStore(load_seed_corpus(DigSiteFS(REAL)))
    fitting, scoring = store.fitting(), store.scoring()
    assert fitting.step_count() == 1831 and scoring.step_count() == 2243
    heldout_ids = {s["trace_id"] for g in scoring.by_split("heldout") for s in g.steps}
    fitting_ids = {s["trace_id"] for _, s in fitting.steps()}
    assert heldout_ids and not heldout_ids & fitting_ids
    assert all(g.split == "train" for g in fitting.games())
    assert not hasattr(fitting, "by_split")            # no way to ask for the other split


def test_fitting_corpus_refuses_heldout_games():
    with pytest.raises(ValueError, match="only hold train"):
        FittingCorpus([make_game("g-1", [], split="heldout")])


def test_trace_store_ids_and_probe_numbering():
    store = TraceStore([make_game("g-1", [pressure_step("gear1", 0, 1)])])
    assert store.exists("t-000001") and not store.exists("t-999999")
    assert store.next_trace_id() == "x-000001" and store.next_trace_id() == "x-000002"
    assert store.exists("x-000002") and store.next_game_id() == "px-0001"
    store.add_game(make_game("px-0007", [pressure_step("gear1", 0, 1)], origin="probe", prefix="x",
                             start=40, split="heldout"))
    assert store.next_trace_id() == "x-000041" and store.next_game_id() == "px-0008"
    assert store.split_of("x-000040") == "heldout" and store.split_of("t-000001") == "train"


def test_games_round_trip_through_disk(tmp_path):
    game = make_game("px-0001", [pressure_step("gear1", 0, 1)], origin="probe", prefix="x")
    save_games(tmp_path / "probe_games.jsonl", [game])
    (loaded,) = load_games(tmp_path / "probe_games.jsonl")
    assert loaded.steps[0]["before"] == game.steps[0]["before"] and loaded.origin == "probe"
    assert load_games(tmp_path / "missing.jsonl") == []


# ---------- checkers on hand-made games ----------

def test_pressure_delta_refutes_and_proposes_the_observed_value():
    game = make_game("g-1", [pressure_step("vent", 8, 4) for _ in range(3)],
                     initial=make_view(pressure=8))
    steps = game.steps
    steps[1]["before"] = make_view(pressure=4)
    steps[1]["events"][1]["value"] = 0           # 4 - 4
    steps[2]["before"] = make_view(pressure=9)
    steps[2]["events"][1]["value"] = 5
    verdict = check_pressure_delta(fit(game), {"card": "vent", "value": -3})
    assert verdict.outcome == "refuted" and len(verdict.counter) == 3
    assert verdict.replacement["value"] == -4


def test_pressure_delta_ignores_wasted_plays_and_floor_cases():
    wasted = {"action": PLAY("gear1"), "events": [{"type": "played", "seat": 0, "card": "gear1"}],
              "view": make_view()}
    game = make_game("g-1", [wasted, pressure_step("vent", 1, 0)], initial=make_view())
    game.steps[1]["before"] = make_view(pressure=1)
    verdict = check_pressure_delta(fit(game), {"card": "gear1", "value": 1})
    assert verdict.outcome == "undecided"                    # the only gear1 play was wasted
    floor = check_pressure_delta(fit(game), {"card": "vent", "value": -3})
    assert floor.outcome == "undecided"                      # 1 -> 0 cannot tell -3 from -4


def test_hand_limit_needs_both_sides_of_the_threshold():
    def step(size, draw):
        legal = [PLAY("vent")] + ([{"type": "draw"}] if draw else [])
        return {"action": legal[0], "legal": legal,
                "view": make_view(), "events": []}
    def game(size, draw, n):
        return make_game(f"g-{n}", [step(size, draw)], initial=make_view(
            hands=[["vent"] * size, ["vent"]], pile=5), start=n * 10)
    rule = {"value": 6}
    only_one_side = check_hand_limit(fit(game(5, True, 1)), rule)
    assert only_one_side.outcome == "undecided"
    both = check_hand_limit(fit(game(5, True, 1), game(6, False, 2)), rule)
    assert both.outcome == "supported"
    wrong = check_hand_limit(fit(game(5, True, 1), game(6, False, 2)), {"value": 7})
    assert wrong.outcome == "refuted" and wrong.replacement["value"] == 6
    assert measure_hand_limit(fit(game(5, True, 1), game(6, False, 2)))["value"] == 6


def jam_game(next_card, next_effect):
    """Seat 0 jams; seat 1 (target) then plays `next_card`."""
    jam = {"action": PLAY("jam"), "seat": 0, "view": make_view(current=1),
           "events": [{"type": "played", "seat": 0, "card": "jam"},
                      {"type": "turn_passed", "to": 1, "actions": 1, "turn": 2}]}
    events = [{"type": "played", "seat": 1, "card": next_card}]
    if next_effect:
        events.append({"type": "pressure", "value": 1})
    nxt = {"action": PLAY(next_card), "seat": 1, "view": make_view(), "events": events}
    return make_game("g-1", [jam, nxt])


def test_jam_checker_judges_only_the_very_next_card():
    assert check_jam(fit(jam_game("gear1", False)), {}).outcome == "supported"
    assert check_jam(fit(jam_game("gear1", True)), {}).outcome == "refuted"
    # The next card is Reverse (no visible effect): the checker must not look further.
    assert check_jam(fit(jam_game("reverse", False)), {}).outcome == "undecided"


def test_reverse_checker_needs_three_players_and_clean_history():
    two = make_game("g-2", [{"action": PLAY("reverse"), "seat": 0,
                             "view": make_view(players=2, current=1),
                             "events": [{"type": "turn_passed", "to": 1, "actions": 1, "turn": 2}]}])
    assert check_reverse(fit(two), {}).outcome == "undecided"
    view = make_view(players=3, current=2)
    ok = make_game("g-3", [{"action": PLAY("reverse"), "seat": 0, "view": view,
                            "events": [{"type": "turn_passed", "to": 2, "actions": 1, "turn": 2}]}],
                   players=3, initial=make_view(players=3))
    assert check_reverse(fit(ok), {}).outcome == "supported"
    bad = make_game("g-4", [{"action": PLAY("reverse"), "seat": 0, "view": make_view(players=3, current=1),
                             "events": [{"type": "turn_passed", "to": 1, "actions": 1, "turn": 2}]}],
                    players=3, initial=make_view(players=3))
    assert check_reverse(fit(bad), {}).outcome == "refuted"


def test_every_checker_survives_an_empty_corpus():
    for kind, checker in CHECKERS.items():
        rule = {"card": "vent", "value": 1, "min": 2, "max": 4, "field": "pressure"}
        assert checker(fit(), rule).outcome == "undecided", kind


# ---------- the audit of the real survey ----------

@pytest.fixture(scope="module")
def audited(tmp_path_factory):
    if not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists():
        pytest.skip("seed corpus not built")
    run_dir = tmp_path_factory.mktemp("audit") / "r"
    fs = DigSiteFS(REAL)
    store = TraceStore(load_seed_corpus(fs))
    ledger = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)
    run_survey(fs, ledger)
    scorer = Scorer(ledger, store)
    report = run_audit(ledger, scorer, store.fitting())
    return ledger, store, report, run_dir


@needs_corpus
def test_the_three_stale_manual_claims_are_refuted_with_real_values(audited):
    ledger, store, report, _ = audited
    refuted = {c.rule["type"] + ":" + c.rule.get("card", ""): c for c in ledger.by_status("refuted")}
    assert set(refuted) == {"hand_limit:", "pressure_delta:vent", "scan_depth:scan"}
    for claim in refuted.values():
        assert claim.source is Source.MANUAL and claim.tier is Tier.DOCUMENTED
        assert claim.evidence and all(store.exists(i) for i in claim.evidence)
    findings = {f.claim_id: f for f in ledger.findings() if f.kind is FindingKind.STALE}
    texts = " ".join(f.traces_said for f in findings.values())
    assert "-4" in texts and "hand limit is 6" in texts and "reveals 1 card" in texts


@needs_corpus
def test_corpus_audit_uses_train_traces_only(audited):
    ledger, store, _, _ = audited
    cited = {i for c in ledger.all_claims() for i in c.evidence}
    cited |= {i for f in ledger.findings() for i in f.trace_ids}
    assert cited and all(store.split_of(i) == "train" for i in cited)


@needs_corpus
def test_nothing_is_confirmed_by_the_corpus_alone(audited):
    ledger, *_ = audited
    assert ledger.counts()["confirmed"] == 0
    assert ledger.counts()["refuted"] == 3 and ledger.counts()["inferred"] > 25


@needs_corpus
def test_measured_replacements_are_recorded_as_trace_claims(audited):
    ledger, *_ = audited
    measured = {c.key: c for c in ledger.by_source(Source.TRACE)}
    assert measured["trace:measured:pressure_delta:vent"].rule["value"] == -4
    assert measured["trace:measured:hand_limit"].rule["value"] == 6
    assert measured["trace:measured:pressure_limit"].rule["value"] == 12
    assert measured["trace:measured:deal_size"].rule["value"] == 4
    assert all(c.status is Status.INFERRED and c.created_by == "auditor" for c in measured.values())


@needs_corpus
def test_unknown_parameters_get_their_measured_value(audited):
    ledger, *_ = audited
    params = {c.rule["name"]: c for c in ledger.all_claims() if c.subkind == "parameter"}
    assert all(c.status is Status.OBSERVED and c.evidence for c in params.values())
    assert any("measured value: 6" in n for n in params["hand_limit"].notes)
    assert any("measured value: 4" in n for n in params["hand_size"].notes)


@needs_corpus
def test_lost_functions_and_missing_stalemate_are_findings(audited):
    ledger, *_ = audited
    kinds = [f.kind for f in ledger.findings()]
    assert kinds.count(FindingKind.MISSING) == 6
    incomplete = [f for f in ledger.findings() if f.kind is FindingKind.INCOMPLETE]
    assert len(incomplete) == 1 and "no winner" in incomplete[0].traces_said


@needs_corpus
def test_audit_leaves_undecided_claims_open_for_probing(audited):
    ledger, _, report, _ = audited
    open_topics = {c.topic for c in ledger.open_questions()}
    assert "function:_gear" in open_topics and "function:_draw" in open_topics
    assert report.undecided and all("corpus cannot decide" in " ".join(
        ledger.get(u["claim_id"]).notes) for u in report.undecided)


@needs_corpus
def test_audit_is_idempotent(audited):
    _, store, _, run_dir = audited
    reloaded = Ledger(run_dir / "ledger.jsonl", trace_validator=store.exists)   # fresh process
    before = (reloaded.counts(), len(reloaded.findings()), len(reloaded.all_claims()))
    run_audit(reloaded, Scorer(reloaded, store), store.fitting())
    assert (reloaded.counts(), len(reloaded.findings()), len(reloaded.all_claims())) == before


# ---------- optional SubQ triage ----------

class FakeTransport:
    def __init__(self):
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append(body)
        options = list(body["questions"]["q"]["criteria"])
        probs = {o: (1.0 if o == options[0] else 0.0) for o in options}
        return 200, {"answers": {"q": {"type": "choice", "choice": options[0],
                                       "probabilities": probs, "confidence": 0.9}},
                     "usage": {"input_tokens": 10}}


@needs_corpus
def test_subq_triage_adds_notes_and_never_changes_status(tmp_path):
    fs = DigSiteFS(REAL)
    store = TraceStore(load_seed_corpus(fs))
    ledger = Ledger(trace_validator=store.exists)
    run_survey(fs, ledger)
    transport = FakeTransport()
    subq = SubqClient("k", transport=transport, sleep=lambda s: None)
    report = run_audit(ledger, Scorer(ledger, store), store.fitting(), subq=subq)
    assert report.triage and transport.requests
    assert any("SubQ triage" in n for c in ledger.all_claims() for n in c.notes)
    assert ledger.counts()["refuted"] == 3                    # triage changed no verdict
    assert "services" not in json.dumps(transport.requests)


# ---------- CLI ----------

@needs_corpus
def test_audit_cli_writes_report_and_updated_probe_list(tmp_path):
    survey_main(["--run-id", "r-cli", "--runs-dir", str(tmp_path)])
    assert audit_main(["--run-id", "r-cli", "--runs-dir", str(tmp_path)]) == 0
    audit = json.loads((tmp_path / "r-cli" / "audit.json").read_text())
    assert audit["refuted"] and len(audit["refuted"]) == 3
    probes = json.loads((tmp_path / "r-cli" / "probe_list.json").read_text())
    assert probes and all(p["priority_class"] <= 3 for p in probes)
    assert probes[0]["class_name"] == "missing_function"
    assert not any(p["subkind"] == "parameter" for p in probes)     # answered by the corpus
