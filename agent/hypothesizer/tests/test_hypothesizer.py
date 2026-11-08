"""T6 tests: propose / score / repair, with Muse replaced by scripted replies."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from auditor.main.corpus import TraceStore, load_seed_corpus
from auditor.main.synthetic import make_game, make_view
from hypothesizer.main.groups import CONTRACT, GROUP_ORDER, GROUPS
from hypothesizer.main.hypothesize import Hypothesizer, confirmed_rules
from hypothesizer.main.prompts import build_prompt
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from llm.main.muse import MuseClient
from sandbox.main.digsite_fs import DigSiteFS
from scorer.main.scorer import Scorer

REAL = Path(__file__).resolve().parents[3] / "dig-site"
DRAW, PASS = {"type": "draw"}, {"type": "pass"}


class ScriptedMuse:
    """Transport that returns queued replies and keeps every prompt it was sent."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []
        self._lock = threading.Lock()

    def __call__(self, url, headers, body, timeout):
        with self._lock:
            self.prompts.append(body["input"])
            reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return 200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                     "usage": {"total_tokens": 10}}


def rules_reply(*codes, names=None):
    return {"rules": [{"name": (names or [f"r{i}"])[i] if names else f"r{i}",
                       "description": "d", "code": code} for i, code in enumerate(codes)]}


def legal_rule(body):
    return f'KIND = "legal"\ndef predict(history, view):\n{body}\n'


ALWAYS_DRAW = legal_rule("    return [{'type': 'draw'}]")
PASS_WHEN_HIGH = legal_rule("    return [{'type': 'pass'}] if view['pressure'] >= 5 else [{'type': 'draw'}]")


def step(pressure, legal):
    return {"action": legal[0], "legal": legal, "view": make_view(pressure=pressure), "events": []}


def build_world():
    store = TraceStore()
    store.add_game(make_game("g-1", [step(0, [DRAW]) for _ in range(3)], split="train"))
    store.add_game(make_game("g-2", [step(0, [DRAW]) for _ in range(4)], split="heldout", start=10))
    store.add_game(make_game("px-0001", [step(6, [PASS])], split="train", origin="probe", prefix="x",
                             initial=make_view(pressure=6), meta={"experiment_id": "e-high"}))
    ledger = Ledger(trace_validator=store.exists)
    ledger.add_claim(text="Manual says vent lowers pressure by 3", source="manual", created_by="s",
                     rule={"type": "pressure_delta", "card": "vent", "value": -3})
    return store, ledger, Scorer(ledger, store, min_heldout=3)


def hypothesizer_for(*replies, rounds=3):
    store, ledger, scorer = build_world()
    transport = ScriptedMuse(*replies)
    muse = MuseClient("k", transport=transport, sleep=lambda s: None)
    return Hypothesizer(muse, scorer, store, ledger, lead_code="# recovered", max_rounds=rounds), \
        store, ledger, transport


# ---------- groups ----------

def test_groups_are_consistent():
    assert set(GROUP_ORDER) == set(GROUPS)
    for name, group in GROUPS.items():
        assert group.kind in ("events", "legal", "view", "setup")
        assert bool(group.covers) == (group.kind == "events"), name
    covered = {t for g in GROUPS.values() for t in g.covers}
    assert covered == {"played", "pressure", "eliminated", "revealed", "drew", "passed",
                       "turn_passed", "game_over"}                      # every event type has a group
    assert "predict(" in CONTRACT and "KIND" in CONTRACT


# ---------- the loop ----------

def test_a_good_rule_survives_and_a_bad_one_is_refuted_in_one_round():
    hyp, _, ledger, transport = hypothesizer_for(rules_reply(ALWAYS_DRAW, PASS_WHEN_HIGH))
    result = hyp.run_group("legal")
    assert result.rounds == 1 and len(result.added) == 2
    assert len(result.report.confirmed) == 1 and len(result.report.refuted) == 1
    confirmed = ledger.get(result.report.confirmed[0])
    assert confirmed.status is Status.CONFIRMED and confirmed.source is Source.MODEL
    assert confirmed.created_by == "muse" and confirmed.rule["group"] == "legal"
    assert "x-000001" in confirmed.evidence


def test_feedback_from_a_failed_round_reaches_the_next_prompt():
    hyp, _, ledger, transport = hypothesizer_for(
        rules_reply(ALWAYS_DRAW), rules_reply(PASS_WHEN_HIGH))
    result = hyp.run_group("legal")
    assert result.rounds == 2 and len(transport.prompts) == 2
    second = transport.prompts[1]
    assert "FEEDBACK FROM THE SCORER" in second and "x-000001" in second
    assert "return [{'type': 'draw'}]" in second                  # its own code, to repair
    assert "expected" in second and "predicted" in second
    assert result.report.confirmed or result.report.inferred


def test_gives_up_after_max_rounds_and_leaves_everything_refuted():
    hyp, _, ledger, transport = hypothesizer_for(
        rules_reply(ALWAYS_DRAW), rules_reply(legal_rule("    return [{'type': 'draw'}, {'type': 'pass'}]")),
        rounds=2)
    result = hyp.run_group("legal")
    assert result.rounds == 2 and not (result.report.inferred or result.report.confirmed)
    assert all(ledger.get(c).status is Status.REFUTED for c in result.added)


def test_held_out_data_never_reaches_a_prompt():
    heldout_overfit = legal_rule(
        "    return [{'type': 'draw'}] if view['pressure'] < 5 else [{'type': 'pass'}]")
    hyp, store, ledger, transport = hypothesizer_for(rules_reply(ALWAYS_DRAW),
                                                     rules_reply(heldout_overfit))
    hyp.run_group("legal")
    heldout_ids = [s["trace_id"] for g in store.games() if g.split == "heldout" for s in g.steps]
    assert heldout_ids
    for prompt in transport.prompts:
        assert not any(i in prompt for i in heldout_ids)


def test_held_out_failure_is_reported_without_detail():
    # Right on train + probe, wrong only on held-out.
    store = TraceStore()
    store.add_game(make_game("g-1", [step(0, [DRAW])], split="train"))
    store.add_game(make_game("g-2", [step(0, [PASS])], split="heldout", start=10))
    ledger = Ledger(trace_validator=store.exists)
    scorer = Scorer(ledger, store)
    transport = ScriptedMuse(rules_reply(ALWAYS_DRAW), rules_reply(ALWAYS_DRAW.replace("draw", "draw ")))
    muse = MuseClient("k", transport=transport, sleep=lambda s: None)
    Hypothesizer(muse, scorer, store, ledger, max_rounds=2).run_group("legal")
    second = transport.prompts[1]
    assert "also_failed_held_out_check" in second and "t-000010" not in second


def test_prompt_has_leads_findings_contract_and_no_service_paths():
    hyp, store, ledger, transport = hypothesizer_for(rules_reply(PASS_WHEN_HIGH))
    first = ledger.all_claims()[0]
    ledger.add_finding(claim_id=first.id, artifact="manual", artifact_said="vent -3",
                       traces_said="vent -4", trace_ids=["t-000001"], kind="stale")
    hyp.run_group("legal")
    prompt = transport.prompts[0]
    for needle in ("TARGET GROUP: legal", "KIND = \"events\"", "[manual/documented/open] Manual says vent",
                   'manual said "vent -3" but traces say: vent -4', "RECOVERED SOURCE FRAGMENT",
                   "WHOLE GAMES"):
        assert needle in prompt.replace("artifact said", "manual said"), needle
    assert "services/" not in prompt and "sealed" not in prompt


def test_other_groups_accepted_rules_are_shown_as_context():
    hyp, _, ledger, transport = hypothesizer_for(rules_reply(PASS_WHEN_HIGH),
                                                 rules_reply('KIND = "view"\ndef predict(h, v, a, e):\n    return None\n'),
                                                 rounds=1)
    hyp.run_group("legal")
    hyp.run_group("view")
    assert "RULES ALREADY ACCEPTED" in transport.prompts[1] and "[legal]" in transport.prompts[1]


def test_proposals_for_the_wrong_group_are_rejected_and_unloadable_ones_kept_as_feedback():
    wrong_kind = 'KIND = "view"\ndef predict(h, v, a, e):\n    return None\n'
    bad_import = "import os\n" + ALWAYS_DRAW
    hyp, _, ledger, transport = hypothesizer_for(
        rules_reply(wrong_kind, bad_import, PASS_WHEN_HIGH))
    result = hyp.run_group("legal")
    assert any("do not match group" in r for r in result.rejected)
    assert any("not allowed" in r for r in result.rejected)
    assert len(result.added) == 2                    # the bad import is kept so the Scorer reports it
    assert len(result.report.unscored) == 1


def test_identical_code_is_not_added_twice():
    hyp, _, ledger, _ = hypothesizer_for(rules_reply(PASS_WHEN_HIGH, PASS_WHEN_HIGH))
    result = hyp.run_group("legal")
    assert len(ledger.by_topic("rule:legal")) == 1 and len(result.added) == 2


def test_muse_failure_is_reported_not_raised():
    hyp, _, _, _ = hypothesizer_for("this is not json", "still not json")
    result = hyp.run_group("legal")
    assert result.error and "valid JSON" in result.error


def test_groups_run_in_parallel_without_corrupting_the_ledger():
    other = 'KIND = "view"\ndef predict(h, v, a, e):\n    return v if False else None\n'
    hyp, _, ledger, _ = hypothesizer_for(rules_reply(PASS_WHEN_HIGH), rules_reply(other), rounds=1)
    results = hyp.run_groups(["legal", "view"], workers=2)
    assert {r.group for r in results} == {"legal", "view"}
    assert len(ledger.by_topic("rule:legal")) + len(ledger.by_topic("rule:view")) == 2


def test_confirmed_rules_are_only_the_confirmed_ones():
    hyp, _, ledger, _ = hypothesizer_for(rules_reply(ALWAYS_DRAW, PASS_WHEN_HIGH))
    hyp.run_group("legal")
    ledger.add_claim(text="manual claim", source="manual", created_by="s")
    rules = confirmed_rules(ledger)
    assert len(rules) == 1 and rules[0]["group"] == "legal" and rules[0]["code"] == PASS_WHEN_HIGH
    assert rules[0]["evidence"] and set(rules[0]) >= {"kind", "covers", "text", "experiments"}


def test_separate_runs_designed_experiments_for_survivors_that_need_one():
    # Two rules the data cannot tell apart -> both inferred, a designed experiment is requested.
    same_a = legal_rule("    return [{'type': 'draw'}]")
    same_b = legal_rule("    return [{'type': 'draw'}]  # variant")
    store = TraceStore()
    store.add_game(make_game("g-1", [step(0, [DRAW])] * 2, split="train"))
    store.add_game(make_game("g-2", [step(0, [DRAW])] * 4, split="heldout", start=10))
    ledger = Ledger(trace_validator=store.exists)
    design = {"experiments": [{"id": "e-m1", "title": "t", "players": 2,
                               "hands": [["vent"] * 4] * 2, "pile": ["vent"] * 4,
                               "actions": [{"type": "play", "card": "vent"}],
                               "claim_topics": [], "rationale": "r"}]}
    transport = ScriptedMuse(rules_reply(same_a, same_b), design)
    muse = MuseClient("k", transport=transport, sleep=lambda s: None)
    hyp = Hypothesizer(muse, Scorer(ledger, store), store, ledger)
    first = hyp.run_group("legal")
    assert not first.report.confirmed and len(first.report.needs_experiment) == 2   # before any attempt

    ran = []

    class FakeRunner:
        def run_experiment(self, exp, hand_size):
            ran.append((exp.id, hand_size))
            store.add_game(make_game("px-0001", [step(0, [DRAW])], split="train", origin="probe",
                                     prefix="x", meta={"experiment_id": exp.id}))
    report = hyp.separate("legal", FakeRunner(), 4)
    assert ran == [("e-m1", 4)]
    assert "Competing hypotheses" in transport.prompts[1]
    # The experiment could not tell them apart, so they are one hypothesis: one representative is
    # confirmed and the twin is recorded as equivalent. Before the attempt, nothing was confirmed.
    assert len(report.confirmed) == 1 and len(report.inferred) == 1
    twin = ledger.get(report.inferred[0])
    assert any("equivalent to" in n for n in twin.notes)


@pytest.mark.skipif(not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists(),
                    reason="seed corpus not built")
def test_prompt_on_the_real_corpus_is_bounded_and_clean():
    fs = DigSiteFS(REAL)
    store = TraceStore(load_seed_corpus(fs))
    ledger = Ledger()
    from surveyor.main.survey import run_survey
    run_survey(fs, ledger)
    for group in GROUP_ORDER:
        prompt = build_prompt(group, corpus=store.fitting(), claims=ledger.all_claims(),
                              findings_text=[], lead_code=fs.read_text("source/engine.py"),
                              accepted=[], feedback=[])
        assert len(prompt) < 120_000, group
        heldout = [s["trace_id"] for g in store.scoring().by_split("heldout") for s in g.steps]
        assert not any(i in prompt for i in heldout[:50]), group
        assert "services/" not in prompt and "DIG_SITE_KEY" not in prompt


# ---------- T6.6: SubQ claim audit ----------

from hypothesizer.main.claim_audit import audit_claims, disagreements
from llm.main.subq import SubqClient, SubqError


class AuditTransport:
    """Answers 'supported' for claims whose text mentions 'good', otherwise 'unsupported'."""

    def __init__(self, fail_on=None):
        self.requests, self.fail_on = [], fail_on

    def __call__(self, url, headers, body, timeout):
        self.requests.append(body)
        state = body["state"]
        if self.fail_on and self.fail_on in state["claim"]:
            return 402, {"detail": {"error_type": "payment_required", "message": "no credits"}}
        label = "supported" if "good" in state["claim"] else "unsupported"
        options = body["questions"]["q"]["criteria"]
        return 200, {"answers": {"q": {"type": "choice", "choice": label,
                                       "probabilities": {o: float(o == label) for o in options},
                                       "confidence": 0.8}}, "usage": {"input_tokens": 20}}


def audit_world():
    store, _, _ = build_world()
    ledger = Ledger(trace_validator=store.exists)
    token = ledger.grant_scorer()
    good = ledger.add_claim(text="good claim", source="manual", created_by="t")
    bad = ledger.add_claim(text="shaky claim", source="manual", created_by="t")
    refuted = ledger.add_claim(text="good but refuted", source="manual", created_by="t")
    ledger.set_status(good.id, "confirmed", ["t-000001"], authority=token)
    ledger.set_status(bad.id, "inferred", ["t-000002"])
    ledger.set_status(refuted.id, "refuted", ["t-000003"], authority=token)
    return store, ledger, good, bad, refuted


def test_claim_audit_flags_disagreements_without_changing_any_status():
    store, ledger, good, bad, refuted = audit_world()
    transport = AuditTransport()
    subq = SubqClient("k", transport=transport, sleep=lambda s: None)
    before = ledger.counts()
    rows = audit_claims(subq, ledger, store)
    assert {r.claim_id: (r.label, r.agrees) for r in rows} == {
        good.id: ("supported", True),            # confirmed + supported: agree
        bad.id: ("unsupported", False),          # inferred but SubQ says unsupported
        refuted.id: ("supported", False),        # refuted but SubQ says it holds
    }
    assert [d["claim_id"] for d in disagreements(rows)] == [bad.id, refuted.id]
    assert ledger.counts() == before                              # statuses untouched
    assert any("SubQ audit disagrees" in n for n in ledger.get(bad.id).notes)
    sent = json.dumps(transport.requests[0]["state"])
    assert "t-000001" in sent and "services/" not in sent         # it was shown the cited trace


def test_claim_audit_skips_claims_without_evidence_and_survives_api_errors():
    store, ledger, good, bad, refuted = audit_world()
    ledger.add_claim(text="no evidence yet", source="manual", created_by="t")
    subq = SubqClient("k", transport=AuditTransport(fail_on="shaky"), sleep=lambda s: None)
    rows = audit_claims(subq, ledger, store)
    assert {r.claim_id for r in rows} == {good.id, refuted.id}    # 'shaky' failed, 'no evidence' skipped
