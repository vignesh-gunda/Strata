"""T11 tests: sections, the 'where the evidence was wrong' table, the sealed mutation check, citations."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator.main.chat import Coordinator
from coordinator.main.reliability import RunState
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from livefeed.main.events import EventLog
from report.main.build import ReportError, build_report, validate_citations
from report.main.cli import main as report_main
from report.main.reveal import MUTATIONS, RevealRefused, check_mutations, read_planted, run_finished

REAL = Path(__file__).resolve().parents[3] / "dig-site"
needs_corpus = pytest.mark.skipif(not (REAL / "traces" / "seed-corpus" / "traces.jsonl").exists(),
                                  reason="dig-site not built")
KNOWN = {"t-000009", "t-000013", "t-000177", "x-000002"}
exists = lambda t: t in KNOWN


def sealed(tmp_path, names, run_id="r-01"):
    d = tmp_path / "sealed"
    d.mkdir(exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "mutation_count": len(names),
                                                  "mutations": names}))
    return d


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "r-r"
    d.mkdir()
    ledger = Ledger(d / "ledger.jsonl")
    manual = ledger.add_claim(text="Vent lowers pressure by 3", source=Source.MANUAL, created_by="t", key="m1")
    measured = ledger.add_claim(text="Vent lowers pressure by 4", source=Source.TRACE, created_by="t", key="m2")
    rule = ledger.add_claim(text="Scan shows the top 2 cards", source=Source.MODEL, created_by="t", key="m3")
    ledger.add_claim(text="What jam does to drawing", source=Source.CODE, created_by="t", key="m4")
    lost = ledger.add_claim(text="The behavior of _draw was lost", source=Source.CODE, created_by="t", key="m5")
    twin = ledger.add_claim(text="Scan reveals two cards", source=Source.MODEL, created_by="t", key="m6")
    auth = ledger.grant_scorer()
    ledger.set_status(manual.id, Status.REFUTED, ["t-000009"], authority=auth)
    ledger.set_status(measured.id, Status.CONFIRMED, ["t-000009", "x-000002"], authority=auth)
    ledger.set_status(rule.id, Status.INFERRED, ["t-000013"])
    ledger.set_status(twin.id, Status.INFERRED, ["t-000013"], reason="equivalent to c-0003 on every step")
    ledger.link_experiment(measured.id, "e-vent-floor")
    ledger.add_finding(claim_id=manual.id, artifact="manual", artifact_said="Vent lowers pressure by 3",
                       traces_said="vent lowers by 4", trace_ids=["t-000009"], kind="stale")
    ledger.add_finding(claim_id=lost.id, artifact="code", artifact_said="_draw body",
                       traces_said="", trace_ids=[], kind="missing")
    return d, ledger


def finish(run_dir):
    state = RunState(run_id=run_dir.name, completed=["survey", "audit", "hypothesize", "rebuild"])
    state.save(run_dir)


# ---------- T11.1 sections ----------
def test_report_has_every_section_in_order(run):
    d, ledger = run
    md = build_report(d, ledger, exists).markdown
    order = ["## Where the evidence was wrong", "## Observed", "## Confirmed", "## Inferred and Open",
             "## Equivalence", "## Mutation check"]
    positions = [md.index(h) for h in order]
    assert positions == sorted(positions)
    assert "### Inferred" in md and "### Open (2:" in md


def test_confirmed_section_lists_evidence_and_experiments(run):
    d, ledger = run
    md = build_report(d, ledger, exists).markdown
    assert "| Vent lowers pressure by 4 `c-0002` | trace | t-000009, x-000002 | e-vent-floor |" in md


def test_open_claims_are_listed_without_traces_and_say_so(run):
    d, ledger = run
    md = build_report(d, ledger, exists).markdown
    assert "- What jam does to drawing `c-0004`" in md and "no trace decides these yet" in md


def test_report_data_matches_the_markdown(run):
    d, ledger = run
    data = build_report(d, ledger, exists).data
    assert data["counts"]["confirmed"] == 1 and data["confirmed"][0]["evidence"] == ["t-000009", "x-000002"]
    assert data["mutations_revealed"] is False and data["mutations"] == []


# ---------- T11.2 where the evidence was wrong ----------
def test_wrong_evidence_table_has_source_verdict_and_trace(run):
    d, ledger = run
    report = build_report(d, ledger, exists)
    assert ("| Manual | Vent lowers pressure by 3 | Refuted (stale) | vent lowers by 4 | t-000009 |"
            in report.markdown)
    assert report.data["wrong_evidence"] == [{
        "artifact": "manual", "claim": "Vent lowers pressure by 3", "verdict": "Refuted", "kind": "stale",
        "traces_said": "vent lowers by 4", "trace_ids": ["t-000009"], "claim_id": "c-0001"}]


def test_lost_information_is_listed_apart_because_no_trace_can_show_it(run):
    d, ledger = run
    md = build_report(d, ledger, exists).markdown
    assert "Information the artifacts lost" in md and "The behavior of _draw was lost `c-0005`" in md
    assert "_draw body" not in md.split("## Observed")[0].split("Refuted")[0]        # not in the evidence table


def test_pipes_in_claim_text_do_not_break_the_table(run):
    d, ledger = run
    c = ledger.add_claim(text="a | b", source=Source.MANUAL, created_by="t", key="pipe")
    ledger.set_status(c.id, Status.INFERRED, ["t-000013"])
    assert "a \\| b" in build_report(d, ledger, exists).markdown


# ---------- T11.4 every claim cites real traces ----------
def test_unknown_trace_ids_fail_the_build(run):
    d, ledger = run
    with pytest.raises(ReportError) as error:
        build_report(d, ledger, lambda t: t != "t-000013")
    assert any("unknown trace t-000013" in p for p in error.value.problems)


def test_a_finding_without_a_trace_fails_the_build():
    ledger = Ledger()
    c = ledger.add_claim(text="x", source=Source.MANUAL, created_by="t")
    # the ledger itself would refuse this, so check the validator directly with a stand-in
    class Loose:
        def all_claims(self): return [c]
        def findings(self):
            return [type("F", (), {"id": "f-1", "kind": type("K", (), {"value": "wrong"})(), "trace_ids": []})()]
    assert validate_citations(Loose(), exists) == ["f-1 cites no trace"]


def test_a_claim_marked_found_in_the_mutation_table_must_cite_traces(run):
    from report.main.reveal import MutationRow
    d, ledger = run
    with pytest.raises(ReportError, match="marked found but cites no trace"):
        build_report(d, ledger, exists, mutation_rows=[MutationRow("vent_four", "x", True, ["c-1"], [])])


def test_nothing_is_written_when_the_build_fails(tmp_path, run):
    d, ledger = run
    with pytest.raises(ReportError):
        build_report(d, ledger, lambda t: False)
    assert not (d / "report.md").exists()


# ---------- T11.3 the sealed mutation check ----------
def test_answer_key_stays_sealed_until_the_run_is_done(run, tmp_path):
    d, ledger = run
    assert not run_finished(d)
    with pytest.raises(RevealRefused, match="has not finished"):
        check_mutations(d, ledger, sealed(tmp_path, ["vent_four"]), "r-01")


def test_once_finished_planted_mutations_are_compared_with_what_was_found(run, tmp_path):
    d, ledger = run
    finish(d)
    assert run_finished(d)
    rows = {r.name: r for r in check_mutations(d, ledger, sealed(tmp_path, ["vent_four", "short_scan", "reverse_skips"]),
                                               "r-01")}
    assert rows["vent_four"].found and rows["vent_four"].claim_ids == ["c-0002"]
    assert rows["vent_four"].trace_ids == ["t-000009", "x-000002"]
    assert not rows["reverse_skips"].found and rows["reverse_skips"].trace_ids == []
    assert not rows["short_scan"].found                      # "top 2 cards" is not the planted "1 card"


def test_a_refuted_manual_claim_alone_is_not_counted_as_finding_the_mutation(tmp_path):
    ledger = Ledger()
    c = ledger.add_claim(text="Vent lowers pressure by 4", source=Source.MANUAL, created_by="t")
    ledger.set_status(c.id, Status.REFUTED, ["t-000009"], authority=ledger.grant_scorer())
    d = tmp_path / "r-x"
    d.mkdir()
    finish(d)
    [row] = check_mutations(d, ledger, sealed(tmp_path, ["vent_four"]), "r-01")
    assert not row.found


def test_mutation_section_shows_up_in_the_report_and_cites_traces(run, tmp_path):
    d, ledger = run
    finish(d)
    rows = check_mutations(d, ledger, sealed(tmp_path, ["vent_four", "hot_restart"]), "r-01")
    md = build_report(d, ledger, exists, mutation_rows=rows).markdown
    assert "| vent lowers pressure by 4 | Yes | c-0002 | t-000009, x-000002 |" in md
    assert "| after an overflow pressure resets to 4 | No | none | none |" in md
    assert "1 of 2 planted change(s) found" in md


def test_without_the_reveal_the_report_names_no_mutation(run):
    d, ledger = run
    md = build_report(d, ledger, exists, mutation_refused="the run has not finished").markdown
    assert "_Not revealed: the run has not finished._" in md
    assert not any(spec["what"] in md for spec in MUTATIONS.values())


def test_the_key_file_is_chosen_by_a_validated_run_id(tmp_path):
    for bad in ("../secret", "a/b", "", "x" * 40):
        with pytest.raises(RevealRefused):
            read_planted(tmp_path, bad)
    with pytest.raises(RevealRefused, match="no answer key"):
        read_planted(tmp_path, "r-99")


def test_planted_entries_may_be_names_or_objects(tmp_path):
    d = sealed(tmp_path, ["vent_four", {"name": "short_scan"}, {"mutation": "hot_restart"}])
    assert read_planted(d, "r-01") == ["vent_four", "short_scan", "hot_restart"]


# ---------- the command and the chat ----------
@needs_corpus
def test_cli_writes_the_report_and_keeps_the_key_sealed_for_an_unfinished_run(tmp_path, capsys):
    runs = tmp_path / "runs"
    ledger = Ledger(runs / "r-t" / "ledger.jsonl")
    c = ledger.add_claim(text="Vent lowers pressure by 3", source=Source.MANUAL, created_by="t")
    ledger.set_status(c.id, Status.INFERRED, ["t-000001"])
    EventLog(runs / "r-t").emit("verify", {"verdict": "Blocked", "games": 12})
    code = report_main(["--run-id", "r-t", "--runs-dir", str(runs), "--sealed-dir", str(sealed(tmp_path, ["vent_four"]))])
    assert code == 0 and "has not finished" in capsys.readouterr().out
    text = (runs / "r-t" / "report.md").read_text()
    assert "# Strata report: run r-t" in text and "Most recent check: Blocked on 12 fuzzed games" in text
    assert json.loads((runs / "r-t" / "report.json").read_text())["mutations_revealed"] is False


@needs_corpus
def test_cli_refuses_to_write_a_report_with_an_unbacked_claim(tmp_path, capsys, monkeypatch):
    runs = tmp_path / "runs"
    ledger = Ledger(runs / "r-t" / "ledger.jsonl")
    c = ledger.add_claim(text="Invented", source=Source.MANUAL, created_by="t")
    ledger.set_status(c.id, Status.INFERRED, ["t-no-such-trace"])        # the unvalidated ledger lets it in
    assert report_main(["--run-id", "r-t", "--runs-dir", str(runs), "--no-reveal"]) == 1
    assert "unknown trace t-no-such-trace" in capsys.readouterr().out
    assert not (runs / "r-t" / "report.md").exists()


def test_cli_without_a_ledger_says_so(tmp_path, capsys):
    assert report_main(["--run-id", "none", "--runs-dir", str(tmp_path)]) == 2


def test_chat_hands_over_the_report_path_and_never_quotes_it(tmp_path, run):
    d, ledger = run
    (d / "report.md").write_text("## Mutation check\nvent lowers pressure by 4 (vent_four)\n")
    chat = Coordinator(tmp_path, d.name, lambda m: None)
    reply = chat.handle("show report")
    assert reply == f"The full report is at {d / 'report.md'}" and "vent_four" not in reply


def test_report_is_the_last_stage_of_a_run(tmp_path, run):
    from coordinator.main.runs import STAGES
    assert STAGES[-1][0] == "report" and STAGES[-1][1] == "report.main.cli"
