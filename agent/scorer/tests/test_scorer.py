"""T6 scorer tests: rules are judged only against traces, by code."""
from __future__ import annotations

from pathlib import Path

import pytest

from auditor.main.corpus import TraceStore, load_seed_corpus
from auditor.main.synthetic import make_game, make_view
from ledger.main.models import Source, Status
from ledger.main.store import Ledger, LedgerError
from sandbox.main.digsite_fs import DigSiteFS
from scorer.main.scorer import Scorer

REAL = Path(__file__).resolve().parents[3] / "dig-site"
needs_corpus = pytest.mark.skipif(not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists(),
                                  reason="seed corpus not built")

DRAW, PASS = {"type": "draw"}, {"type": "pass"}


def legal_step(pressure, legal):
    view = make_view(pressure=pressure)
    return {"action": legal[0], "legal": legal, "view": view, "events": []}


def rule(body: str) -> str:
    return f'KIND = "legal"\ndef predict(history, view):\n{body}\n'


ALWAYS_DRAW = rule("    return [{'type': 'draw'}]")
PASS_WHEN_HIGH = rule("    return [{'type': 'pass'}] if view['pressure'] >= 5 else [{'type': 'draw'}]")


def build(with_probe=True, probe_separates=True):
    """train + heldout steps where draw is legal, plus a probe step where only pass is."""
    store = TraceStore()
    store.add_game(make_game("g-1", [legal_step(0, [DRAW]) for _ in range(3)], split="train"))
    store.add_game(make_game("g-2", [legal_step(0, [DRAW]) for _ in range(4)], split="heldout",
                             start=10))
    if with_probe:
        pressure = 6 if probe_separates else 0
        legal = [PASS] if probe_separates else [DRAW]
        store.add_game(make_game("px-0001", [legal_step(pressure, legal)], split="train",
                                 origin="probe", prefix="x", initial=make_view(pressure=pressure),
                                 meta={"experiment_id": "e-high"}))
    ledger = Ledger(trace_validator=store.exists)
    return store, ledger, Scorer(ledger, store, min_heldout=3)


def add_rule(ledger, code, name, group="legal"):
    return ledger.add_claim(text=name, source=Source.MODEL, created_by="test", key=name,
                            rule={"type": "executable", "group": group, "code": code})


def test_only_one_scorer_per_ledger():
    store, ledger, _ = build()
    with pytest.raises(LedgerError, match="already granted"):
        Scorer(ledger, store)


def test_correct_rule_is_confirmed_wrong_rule_refuted_with_trace_ids():
    store, ledger, scorer = build()
    good = add_rule(ledger, PASS_WHEN_HIGH, "good")
    bad = add_rule(ledger, ALWAYS_DRAW, "bad")
    report = scorer.adjudicate_group("legal")
    assert report.confirmed == [good.id] and report.refuted == [bad.id]
    refuted = ledger.get(bad.id)
    assert refuted.status is Status.REFUTED and refuted.evidence == ["x-000001"]
    confirmed = ledger.get(good.id)
    assert confirmed.status is Status.CONFIRMED
    assert "x-000001" in confirmed.evidence                       # the separating experiment
    assert any(store.split_of(i) == "heldout" for i in confirmed.evidence)
    assert confirmed.experiment_ids == ["e-high"]


def test_no_probe_separation_means_inferred_and_needs_an_experiment():
    _, ledger, scorer = build(with_probe=False)
    a = add_rule(ledger, ALWAYS_DRAW, "a")
    b = add_rule(ledger, PASS_WHEN_HIGH, "b")        # identical on this data
    report = scorer.adjudicate_group("legal")
    assert report.confirmed == [] and sorted(report.inferred) == sorted([a.id, b.id])
    assert sorted(report.needs_experiment) == sorted([a.id, b.id])
    assert ledger.get(a.id).status is Status.INFERRED


def test_a_probe_that_does_not_separate_does_not_confirm():
    _, ledger, scorer = build(probe_separates=False)
    add_rule(ledger, ALWAYS_DRAW, "a")
    add_rule(ledger, PASS_WHEN_HIGH, "b")
    assert scorer.adjudicate_group("legal").confirmed == []


def test_equivalent_survivors_are_one_hypothesis_confirmed_only_after_a_separation_attempt():
    _, ledger, scorer = build(probe_separates=False)
    a = add_rule(ledger, ALWAYS_DRAW, "a")
    b = add_rule(ledger, ALWAYS_DRAW + "# same behavior, different text\n", "b")
    assert scorer.adjudicate_group("legal").confirmed == []                 # default: no collapse
    report = scorer.adjudicate_group("legal", collapse_equivalent=True)
    assert report.confirmed == [a.id] and report.inferred == [b.id]
    assert ledger.get(a.id).status is Status.CONFIRMED
    twin = ledger.get(b.id)
    assert twin.status is Status.INFERRED and any("equivalent to " + a.id in n for n in twin.notes)
    assert report.needs_experiment == []


def test_rules_that_differ_anywhere_are_not_collapsed():
    _, ledger, scorer = build(probe_separates=False)
    always = add_rule(ledger, ALWAYS_DRAW, "always")
    only_low = add_rule(ledger, rule("    return [{'type': 'draw'}] if len(history) == 0 else None"), "first_step_only")
    report = scorer.adjudicate_group("legal", collapse_equivalent=True)
    assert report.confirmed == [] and sorted(report.needs_experiment) == sorted([always.id, only_low.id])


def test_lone_rule_needs_an_applicable_probe_step():
    _, ledger, scorer = build(with_probe=False)
    add_rule(ledger, ALWAYS_DRAW, "only")
    assert scorer.adjudicate_group("legal").confirmed == []
    _, ledger2, scorer2 = build(probe_separates=False)
    only = add_rule(ledger2, ALWAYS_DRAW, "only")
    assert scorer2.adjudicate_group("legal").confirmed == [only.id]


def test_too_few_heldout_steps_blocks_confirmation():
    store, ledger, _ = build()
    strict = Scorer.__new__(Scorer)                  # a second scorer needs its own ledger
    ledger2 = Ledger(trace_validator=store.exists)
    strict = Scorer(ledger2, store, min_heldout=5)
    add_rule(ledger2, PASS_WHEN_HIGH, "good")
    report = strict.adjudicate_group("legal")
    assert report.confirmed == [] and len(report.inferred) == 1


def test_heldout_counterexample_refutes_and_is_cited():
    store = TraceStore()
    store.add_game(make_game("g-1", [legal_step(0, [DRAW])], split="train"))
    store.add_game(make_game("g-2", [legal_step(0, [PASS])], split="heldout", start=5))
    ledger = Ledger(trace_validator=store.exists)
    scorer = Scorer(ledger, store)
    claim = add_rule(ledger, ALWAYS_DRAW, "overfit")
    scorer.adjudicate_group("legal")
    assert ledger.get(claim.id).status is Status.REFUTED
    assert ledger.get(claim.id).evidence == ["t-000005"]


def test_rule_that_never_applies_stays_open():
    _, ledger, scorer = build()
    claim = add_rule(ledger, rule("    return None"), "silent")
    report = scorer.adjudicate_group("legal")
    assert report.unscored == [claim.id] and ledger.get(claim.id).status is Status.OPEN


def test_rule_that_does_not_load_is_unscored_not_refuted():
    _, ledger, scorer = build()
    claim = add_rule(ledger, "import os\n" + ALWAYS_DRAW, "evil")
    report = scorer.adjudicate_group("legal")
    assert report.unscored == [claim.id] and ledger.get(claim.id).status is Status.OPEN
    assert "not allowed" in report.scores[claim.id].load_error


def test_crashing_rule_counts_as_a_mismatch():
    _, ledger, scorer = build()
    claim = add_rule(ledger, rule("    raise RuntimeError('bug')"), "crashy")
    scorer.adjudicate_group("legal")
    assert ledger.get(claim.id).status is Status.REFUTED
    assert "RuntimeError" in ledger.get(claim.id).notes[-1]


def test_confirmed_rule_that_later_gets_a_counterexample_is_refuted():
    store, ledger, scorer = build()
    good = add_rule(ledger, PASS_WHEN_HIGH, "good")
    add_rule(ledger, ALWAYS_DRAW, "bad")
    scorer.adjudicate_group("legal")
    assert ledger.get(good.id).status is Status.CONFIRMED
    store.add_game(make_game("px-0002", [legal_step(0, [PASS])], split="train", origin="probe",
                             prefix="x", start=2))
    scorer.adjudicate_group("legal")
    assert ledger.get(good.id).status is Status.REFUTED


def test_rules_in_other_groups_are_untouched():
    _, ledger, scorer = build()
    other = add_rule(ledger, ALWAYS_DRAW, "other", group="view")
    scorer.adjudicate_group("legal")
    assert ledger.get(other.id).status is Status.OPEN


def test_refute_helper_is_idempotent_and_needs_traces():
    _, ledger, scorer = build()
    claim = ledger.add_claim(text="manual claim", source="manual", created_by="t")
    with pytest.raises(LedgerError, match="trace ID"):
        scorer.refute(claim.id, [], "no evidence")
    scorer.refute(claim.id, ["t-000001"], "x")
    scorer.refute(claim.id, ["t-000001"], "again")             # no error
    assert ledger.get(claim.id).status is Status.REFUTED


# ---------- against the real corpus ----------

CORRECT_LEGAL = '''
KIND = "legal"
LIMIT = {limit}
def predict(history, view):
    if view["over"]:
        return []
    hand = view["hands"][view["current_seat"]]
    acts = [{{"type": "play", "card": c}} for c in sorted(set(hand))]
    if view["draw_pile_size"] > 0 and len(hand) < LIMIT:
        acts.append({{"type": "draw"}})
    return acts or [{{"type": "pass"}}]
'''


@needs_corpus
def test_real_corpus_legal_rule_with_the_true_limit_has_no_mismatch():
    store = TraceStore(load_seed_corpus(DigSiteFS(REAL)))
    ledger = Ledger(trace_validator=store.exists)
    scorer = Scorer(ledger, store)
    score = scorer.score_rule(CORRECT_LEGAL.format(limit=6))
    assert score.mismatches == 0 and score.applicable == {"train": 1831, "probe": 0, "heldout": 412}


@needs_corpus
def test_real_corpus_manual_limit_is_refuted_and_true_limit_survives():
    store = TraceStore(load_seed_corpus(DigSiteFS(REAL)))
    ledger = Ledger(trace_validator=store.exists)
    scorer = Scorer(ledger, store, require_probe_separation=False)
    manual = add_rule(ledger, CORRECT_LEGAL.format(limit=7), "limit7")
    truth = add_rule(ledger, CORRECT_LEGAL.format(limit=6), "limit6")
    report = scorer.adjudicate_group("legal")
    assert report.refuted == [manual.id] and report.confirmed == [truth.id]
    assert ledger.get(manual.id).evidence and all(store.exists(i) for i in ledger.get(manual.id).evidence)


@needs_corpus
def test_real_corpus_setup_rule():
    store = TraceStore(load_seed_corpus(DigSiteFS(REAL)))
    scorer = Scorer(Ledger(trace_validator=store.exists), store)
    code = '''
KIND = "setup"
def predict(players, deck):
    n = 4 * players
    return {"players": players, "hands": [deck[s:n:players] for s in range(players)],
            "alive": [True] * players, "discard": [], "draw_pile_size": len(deck) - n,
            "current_seat": 0, "actions_left": 1, "pressure": 0, "turn": 1,
            "over": False, "winner": None}
'''
    score = scorer.score_rule(code)
    assert score.kind == "setup" and score.mismatches == 0 and sum(score.applicable.values()) == 40
