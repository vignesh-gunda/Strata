"""T16.10: a difference between the rebuild and the original lowers trust and opens a question."""
import json

from ledger.main.models import Status
from ledger.main.store import Ledger
from rebuilder.main.feedback import apply_divergence_feedback, groups_touched


def world():
    ledger = Ledger(trace_validator=lambda t: True)
    token = ledger.grant_scorer()
    rules = []
    for group in ("pressure", "drew", "legal"):
        claim = ledger.add_claim(text=f"{group} rule", source="model", created_by="t")
        ledger.set_status(claim.id, "confirmed", ["t-000001"], authority=token)
        rules.append({"claim_id": claim.id, "group": group})
    return ledger, rules


def events(*types):
    return {"events": [{"type": t} for t in types], "view": {}}


FUZZ = {"source": "fuzz", "kind": "act", "actions": [{"type": "draw"}, {"type": "pass"}],
        "reference": events("played", "pressure"), "candidate": events("played", "eliminated")}


def test_the_groups_come_from_the_events_that_differ():
    assert groups_touched(FUZZ) == ["pressure", "eliminated"]
    assert groups_touched({"kind": "new_game", "reference": {}, "candidate": {}}) == ["setup"]
    assert groups_touched({"what": "legal actions"}) == ["legal"]
    same_events = {"kind": "act", "reference": {**events("drew"), "view": {"pressure": 1}},
                   "candidate": {**events("drew"), "view": {"pressure": 2}}}
    assert groups_touched(same_events) == ["view"]
    assert groups_touched({"kind": "build", "reference": None, "candidate": ["syntax"]}) == []


def test_only_the_rules_for_the_part_that_differed_are_demoted(tmp_path):
    ledger, rules = world()
    probes = tmp_path / "probe_list.json"
    probes.write_text(json.dumps([{"rank": 1, "claim_key": "old", "topic": "function:_draw"}]))
    summary = apply_divergence_feedback(ledger, rules, FUZZ, trace_id="x-000009", probe_list_path=probes)
    assert summary["demoted"] == [rules[0]["claim_id"]]                    # pressure only
    assert ledger.get(rules[0]["claim_id"]).status is Status.INFERRED
    assert ledger.get(rules[1]["claim_id"]).status is Status.CONFIRMED
    assert ledger.get(rules[2]["claim_id"]).status is Status.CONFIRMED
    assert "x-000009" in ledger.get(rules[0]["claim_id"]).notes[-1]


def test_the_difference_becomes_an_open_claim_and_the_first_probe(tmp_path):
    ledger, rules = world()
    probes = tmp_path / "probe_list.json"
    probes.write_text(json.dumps([{"rank": 1, "claim_key": "old", "topic": "function:_draw"}]))
    summary = apply_divergence_feedback(ledger, rules, FUZZ, trace_id="x-000009", probe_list_path=probes)
    assert len(summary["opened"]) == 2
    opened = ledger.get(summary["opened"][0])
    assert opened.status is Status.OPEN and opened.topic == "divergence:pressure" and "x-000009" in opened.text
    items = json.loads(probes.read_text())
    assert [i["rank"] for i in items] == [1, 2, 3]
    assert items[0]["class_name"] == "divergence" and items[-1]["claim_key"] == "old"


def test_the_same_difference_is_not_added_twice(tmp_path):
    ledger, rules = world()
    probes = tmp_path / "probe_list.json"
    apply_divergence_feedback(ledger, rules, FUZZ, trace_id=None, probe_list_path=probes)
    again = apply_divergence_feedback(ledger, rules, FUZZ, trace_id=None, probe_list_path=probes)
    assert len(json.loads(probes.read_text())) == 2 and again["demoted"] == []
    assert len(ledger.open_questions()) == 2


def test_a_build_failure_changes_nothing(tmp_path):
    ledger, rules = world()
    before = ledger.counts()
    summary = apply_divergence_feedback(ledger, rules, {"source": "build", "kind": "build", "actions": [],
                                                        "reference": None, "candidate": ["syntax"]},
                                        trace_id=None, probe_list_path=tmp_path / "p.json")
    assert summary["groups"] == [] and ledger.counts() == before and not (tmp_path / "p.json").exists()


def test_nothing_here_can_confirm_or_refute(tmp_path):
    ledger, rules = world()
    apply_divergence_feedback(ledger, rules, FUZZ, trace_id=None, probe_list_path=None)
    assert ledger.counts().get("refuted", 0) == 0
