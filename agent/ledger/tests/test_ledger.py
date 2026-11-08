"""T2 tests: every illegal transition is rejected, and the ledger survives a restart."""
from __future__ import annotations

import pytest

from ledger.main.models import FindingKind, Source, Status, Tier
from ledger.main.store import Ledger, LedgerError


def make(ledger: Ledger, source="manual", text="Vent lowers pressure by 3", **kw):
    return ledger.add_claim(text=text, source=source, created_by="test", **kw)


# ---------- creation ----------

def test_new_claims_are_open_with_source_and_tier():
    ledger = Ledger()
    manual = make(ledger, "manual")
    code = make(ledger, "code", text="draw limited by hand_limit")
    model = make(ledger, "model", text="guess")
    assert manual.status is Status.OPEN and manual.tier is Tier.DOCUMENTED
    assert code.tier is Tier.READ_FROM_CODE and code.source is Source.CODE
    assert model.tier is Tier.INFERRED
    assert [manual.id, code.id, model.id] == ["c-0001", "c-0002", "c-0003"]


def test_repeated_key_returns_existing_claim_without_duplicating():
    ledger = Ledger()
    a = make(ledger, key="manual:vent")
    b = make(ledger, key="manual:vent")
    assert a.id == b.id and len(ledger.all_claims()) == 1


def test_rejects_empty_text_and_rule_without_type():
    ledger = Ledger()
    with pytest.raises(LedgerError):
        make(ledger, text="   ")
    with pytest.raises(LedgerError):
        make(ledger, rule={"value": 1})


# ---------- evidence rule ----------

@pytest.mark.parametrize("status", ["observed", "inferred"])
def test_status_without_trace_ids_is_rejected(status):
    ledger = Ledger()
    claim = make(ledger)
    with pytest.raises(LedgerError, match="trace ID"):
        ledger.set_status(claim.id, status, [])
    assert ledger.get(claim.id).status is Status.OPEN


def test_cannot_set_back_to_open():
    ledger = Ledger()
    claim = make(ledger)
    ledger.set_status(claim.id, "inferred", ["t-000001"])
    with pytest.raises(LedgerError):
        ledger.set_status(claim.id, "open", ["t-000001"])


def test_trace_validator_rejects_unknown_ids():
    ledger = Ledger(trace_validator=lambda t: t == "t-000001")
    claim = make(ledger)
    ledger.set_status(claim.id, "inferred", ["t-000001"])
    with pytest.raises(LedgerError, match="unknown trace IDs"):
        ledger.set_status(claim.id, "inferred", ["t-999999"])


# ---------- scorer authority ----------

def test_manual_claim_cannot_be_confirmed_without_authority():
    ledger = Ledger()
    claim = make(ledger, "manual")
    with pytest.raises(LedgerError, match="only the scorer"):
        ledger.set_status(claim.id, "confirmed", ["t-000001"])
    with pytest.raises(LedgerError, match="only the scorer"):
        ledger.set_status(claim.id, "refuted", ["t-000001"])


def test_model_cannot_forge_authority():
    ledger = Ledger()
    claim = make(ledger, "model")
    ledger.grant_scorer()
    from ledger.main.store import ScorerAuthority
    with pytest.raises(LedgerError, match="only the scorer"):
        ledger.set_status(claim.id, "confirmed", ["t-000001"], authority=ScorerAuthority())
    with pytest.raises(LedgerError, match="only the scorer"):
        ledger.set_status(claim.id, "confirmed", ["t-000001"], authority="scorer")


def test_scorer_authority_is_granted_once_and_per_ledger():
    one, two = Ledger(), Ledger()
    token = one.grant_scorer()
    with pytest.raises(LedgerError, match="already granted"):
        one.grant_scorer()
    claim = make(two)
    with pytest.raises(LedgerError, match="only the scorer"):  # token from another ledger
        two.set_status(claim.id, "confirmed", ["t-1"], authority=token)


def test_scorer_confirms_with_traces_and_tier_upgrades_to_observed():
    ledger = Ledger()
    token = ledger.grant_scorer()
    claim = make(ledger, "manual")
    done = ledger.set_status(claim.id, "confirmed", ["t-000412"], authority=token, reason="e-05")
    assert done.status is Status.CONFIRMED and done.tier is Tier.OBSERVED
    assert done.source is Source.MANUAL          # origin never changes
    assert done.evidence == ["t-000412"]
    with pytest.raises(LedgerError, match="trace ID"):
        ledger.set_status(make(ledger, key="x2").id, "confirmed", [], authority=token)


def test_refuted_is_final_and_keeps_its_tier_and_evidence():
    ledger = Ledger()
    token = ledger.grant_scorer()
    claim = make(ledger, "manual")
    ledger.set_status(claim.id, "refuted", ["t-000412"], authority=token)
    got = ledger.get(claim.id)
    assert got.status is Status.REFUTED and got.tier is Tier.DOCUMENTED
    for target in ("confirmed", "inferred", "observed", "refuted"):
        with pytest.raises(LedgerError, match="cannot move"):
            ledger.set_status(claim.id, target, ["t-2"], authority=token)


def test_confirmed_can_still_be_refuted_by_a_counterexample():
    ledger = Ledger()
    token = ledger.grant_scorer()
    claim = make(ledger)
    ledger.set_status(claim.id, "confirmed", ["t-1"], authority=token)
    ledger.set_status(claim.id, "refuted", ["t-2"], authority=token)
    assert ledger.get(claim.id).evidence == ["t-1", "t-2"]


def test_confirmed_cannot_drop_back_via_set_status():
    ledger = Ledger()
    token = ledger.grant_scorer()
    claim = make(ledger)
    ledger.set_status(claim.id, "confirmed", ["t-1"], authority=token)
    with pytest.raises(LedgerError, match="cannot move"):
        ledger.set_status(claim.id, "inferred", ["t-1"])


# ---------- demotion ----------

def test_unretested_confirmed_claims_fall_back_to_inferred():
    ledger = Ledger()
    token = ledger.grant_scorer()
    kept, dropped = make(ledger, key="a"), make(ledger, key="b")
    for claim in (kept, dropped):
        ledger.set_status(claim.id, "confirmed", ["t-1"], authority=token)
    ledger.mark_retested(kept.id, ["t-77"])
    assert ledger.demote_unretested() == [dropped.id]
    assert ledger.get(kept.id).status is Status.CONFIRMED
    assert ledger.get(dropped.id).status is Status.INFERRED
    with pytest.raises(LedgerError):
        ledger.demote(make(ledger, key="c").id, "x")   # only confirmed claims demote


# ---------- findings ----------

def test_findings_need_traces_except_missing():
    ledger = Ledger()
    claim = make(ledger)
    with pytest.raises(LedgerError, match="trace IDs"):
        ledger.add_finding(claim_id=claim.id, artifact="manual", artifact_said="-3",
                           traces_said="-4", trace_ids=[], kind="stale")
    finding = ledger.add_finding(claim_id=claim.id, artifact="manual", artifact_said="-3",
                                 traces_said="-4", trace_ids=["t-000412"], kind=FindingKind.STALE)
    lost = ledger.add_finding(claim_id=claim.id, artifact="code", artifact_said="(body lost)",
                              traces_said="", trace_ids=[], kind="missing")
    assert [f.id for f in ledger.findings(claim.id)] == [finding.id, lost.id]
    with pytest.raises(LedgerError):
        ledger.add_finding(claim_id="c-9999", artifact="x", artifact_said="", traces_said="",
                           trace_ids=["t-1"], kind="wrong")


# ---------- predictions ----------

def test_predictions_are_logged_first_and_outcomes_separately():
    ledger = Ledger()
    claim = make(ledger)
    p = ledger.log_prediction(game_id="g-1", step=3, prediction={"seat_1_out": False},
                              claim_ids=[claim.id])
    assert p.id == "p-000001" and ledger.outcome(p.id) is None
    out = ledger.record_outcome(p.id, {"seat_1_out": True}, surprise=True, trace_id="t-000009")
    assert out.surprise and ledger.surprises()[0][0].prediction == {"seat_1_out": False}
    with pytest.raises(LedgerError, match="already recorded"):
        ledger.record_outcome(p.id, {"seat_1_out": False}, surprise=False)
    with pytest.raises(LedgerError):
        ledger.record_outcome("p-424242", {}, surprise=False)
    with pytest.raises(LedgerError):
        ledger.log_prediction(game_id="g", step=1, prediction={}, claim_ids=["c-9999"])
    assert ledger.predictions()[0].prediction == {"seat_1_out": False}  # unchanged


# ---------- persistence ----------

def test_ledger_survives_restart(tmp_path):
    path = tmp_path / "run" / "ledger.jsonl"
    first = Ledger(path)
    token = first.grant_scorer()
    a = make(first, key="a", topic="card:vent", rule={"type": "pressure_delta", "value": -3})
    b = make(first, "code", text="hand limit", key="b")
    first.set_status(a.id, "refuted", ["t-000412"], authority=token, reason="traces show -4")
    first.link_experiment(b.id, "e-03")
    first.add_note(b.id, "probe hardest")
    first.add_finding(claim_id=a.id, artifact="manual", artifact_said="-3", traces_said="-4",
                      trace_ids=["t-000412"], kind="stale")
    p = first.log_prediction(game_id="g-1", step=1, prediction={"x": 1})
    first.record_outcome(p.id, {"x": 2}, surprise=True)

    second = Ledger(path)
    assert [c.id for c in second.all_claims()] == [a.id, b.id]
    assert second.get(a.id).status is Status.REFUTED
    assert second.get(a.id).evidence == ["t-000412"]
    assert second.get(b.id).experiment_ids == ["e-03"] and second.get(b.id).notes == ["probe hardest"]
    assert len(second.findings()) == 1 and len(second.surprises()) == 1
    assert second.get_by_key("a").id == a.id
    # A restarted ledger keeps enforcing rules and continues numbering.
    with pytest.raises(LedgerError, match="only the scorer"):
        second.set_status(b.id, "confirmed", ["t-1"])
    assert make(second, key="c").id == "c-0003"
    assert Ledger(path).get("c-0003").key == "c"


# ---------- queries ----------

def test_queries_by_status_tier_source_topic_and_counts():
    ledger = Ledger()
    token = ledger.grant_scorer()
    a = make(ledger, "manual", key="a", topic="card:vent")
    b = make(ledger, "code", text="code claim", key="b")
    c = make(ledger, "model", text="model claim", key="c")
    ledger.set_status(a.id, "confirmed", ["t-1"], authority=token)
    ledger.set_status(c.id, "inferred", ["t-2"])
    assert [x.id for x in ledger.by_status("confirmed")] == [a.id]
    assert [x.id for x in ledger.open_questions()] == [b.id]
    assert [x.id for x in ledger.by_source("code")] == [b.id]
    assert [x.id for x in ledger.by_tier("read_from_code")] == [b.id]
    assert [x.id for x in ledger.by_tier("observed")] == [a.id]   # upgraded on confirmation
    assert [x.id for x in ledger.by_topic("card:vent")] == [a.id]
    assert ledger.counts() == {"observed": 0, "inferred": 1, "confirmed": 1,
                               "refuted": 0, "open": 1}


def test_returned_claims_are_copies():
    ledger = Ledger()
    claim = make(ledger)
    claim.status = Status.CONFIRMED                      # mutate the copy
    ledger.get(claim.id).evidence.append("t-forged")     # and another copy
    assert ledger.get(claim.id).status is Status.OPEN
    assert ledger.get(claim.id).evidence == []
    assert ledger.all_claims()[0].status is Status.OPEN
