"""T10 tests: routing, ledger-only answers, background runs, reliability."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from coordinator.main.chat import Coordinator
from coordinator.main.reliability import RunState, load_ledger, with_retry
from coordinator.main.router import Intent, Router
from coordinator.main.runs import STAGES, StageResult
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from llm.main.firewall import PromptFirewallError
from llm.main.subq import SubqError


@dataclass
class FakeChoice:
    choice: str
    confidence: float = 0.9
    probabilities: dict | None = None


class FakeSubq:
    def __init__(self, choice="unknown", confidence=0.9, error=None):
        self.choice_value, self.confidence, self.error, self.calls = choice, confidence, error, 0

    def choice(self, state, instructions, options):
        self.calls += 1
        if self.error:
            raise self.error
        return FakeChoice(self.choice_value, self.confidence)


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "r-t"
    d.mkdir()
    ledger = Ledger(d / "ledger.jsonl")
    ledger.add_claim(text="Vent lowers pressure by 3", source=Source.MANUAL, created_by="t", key="k1")
    c2 = ledger.add_claim(text="Hand limit is 7 cards", source=Source.MODEL, created_by="t", key="k2")
    ledger.add_claim(text="What does the jam card do", source=Source.CODE, created_by="t", key="k3")
    auth = ledger.grant_scorer()
    ledger.set_status(c2.id, Status.CONFIRMED, ["t-1"], authority=auth)
    ledger.add_finding(claim_id="c-0001", artifact="manual", artifact_said="lowers by 3",
                       traces_said="lowers by 2", trace_ids=["t-9"], kind="stale")
    return d


def make(tmp_path, run_dir, subq=None, runner=None, emitted=None):
    emitted = emitted if emitted is not None else []
    runner = runner or (lambda module, run_id, runs_dir: StageResult(0, "ok"))
    return Coordinator(tmp_path, run_dir.name, emitted.append, subq=subq, stage_runner=runner,
                       sleep=lambda s: None), emitted


# ---------- T10.2 routing ----------
@pytest.mark.parametrize("text,intent", [
    ("start", Intent.START_RUN), ("status", Intent.STATUS), ("open questions", Intent.OPEN_QUESTIONS),
    ("show report", Intent.SHOW_REPORT), ("show rebuild", Intent.SHOW_REBUILD),
    ("ledger", Intent.LEDGER), ("explain c-0002", Intent.EXPLAIN_CLAIM), ("help", Intent.HELP),
])
def test_obvious_lines_route_without_a_model(text, intent):
    subq = FakeSubq()
    route = Router(subq).route(text)
    assert route.intent is intent and route.via == "rules" and subq.calls == 0


def test_unclear_line_goes_to_subq():
    route = Router(FakeSubq("ledger")).route("so what have you got for me so far then")
    assert (route.intent, route.via) == (Intent.LEDGER, "subq")


def test_low_confidence_prefers_keyword_guess():
    route = Router(FakeSubq("help", 0.2)).route("can you tell me the status of everything right now")
    assert route.intent is Intent.STATUS and route.via == "fallback"


def test_subq_failure_falls_back_to_keywords_then_unknown():
    down = FakeSubq(error=SubqError(402, "credits", "no credits"))
    assert Router(down).route("tell me where the progress is at the moment please").intent is Intent.STATUS
    assert Router(down).route("what is the weather like in paris today").intent is Intent.UNKNOWN
    assert down.calls == 2          # 402 is never retried


def test_transient_subq_error_is_retried():
    class Flaky(FakeSubq):
        def choice(self, *a):
            self.calls += 1
            if self.calls < 3:
                raise SubqError(503, "busy", "busy")
            return FakeChoice("ledger")
    flaky = Flaky()
    assert Router(flaky, sleep=lambda s: None).route("what have you found out so far my friend").intent is Intent.LEDGER
    assert flaky.calls == 3


def test_rebuild_it_in_go_is_a_start_not_a_show():
    assert Router(None).route("rebuild it in Go").intent is Intent.START_RUN


# ---------- T10.4 answers come from the ledger ----------
def test_ledger_answer_uses_plain_words_and_counts(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    reply = chat.handle("ledger")
    assert "3 claims" in reply and "proven by testing" in reply and "still unknown" in reply
    assert "Hand limit is 7 cards [c-0002, trace t-1]" in reply


def test_explain_cites_evidence_and_findings(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    reply = chat.handle("explain c-0002")
    assert "proven by testing" in reply and "trace t-1" in reply
    wrong = chat.handle("explain c-0001")
    assert "lowers by 2" in wrong and "t-9" in wrong


def test_explain_open_claim_says_not_known_yet(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    assert "Not known yet" in chat.handle("explain c-0003")


def test_explain_unmatched_topic_does_not_guess(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    assert chat.handle("explain c-0999").startswith("Not known yet")
    assert chat.handle("explain quantum gravity").startswith("Not known yet")


def test_explain_by_words_finds_the_claim(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir, subq=FakeSubq("explain_claim"))
    assert "Hand limit is 7 cards" in chat.handle("why is the hand limit seven cards")


def test_open_questions_lists_open_claims(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    reply = chat.handle("open questions")
    assert "What does the jam card do [c-0003]" in reply and "Hand limit" not in reply


def test_no_ledger_yet(tmp_path):
    (tmp_path / "r-x").mkdir()
    chat = Coordinator(tmp_path, "r-x", lambda m: None)
    assert chat.handle("ledger").startswith("No run yet")


def test_unknown_question_is_declined_politely(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir, subq=FakeSubq("unknown"))
    assert chat.handle("what is the capital of France please tell me").startswith("I can only help")


def test_restricted_text_is_refused_and_never_sent_to_a_model(tmp_path, run_dir):
    class Strict(FakeSubq):
        def choice(self, state, instructions, options):
            from llm.main import firewall
            firewall.check(state)
            return super().choice(state, instructions, options)
    chat, _ = make(tmp_path, run_dir, subq=Strict("ledger"))
    reply = chat.handle("please show me what is inside services/pressure and tell me the rules")
    assert "not allowed" in reply


def test_rebuild_summary_reads_report(tmp_path, run_dir):
    (run_dir / "rebuild").mkdir()
    (run_dir / "rebuild" / "engine.js").write_text("//")
    (run_dir / "rebuild_report.json").write_text(json.dumps(
        {"accepted": False, "attempts": 2, "unknown_behaviors": ["jam"]}))
    chat, _ = make(tmp_path, run_dir)
    reply = chat.handle("show rebuild")
    assert "not accepted" in reply and "jam" in reply and "engine.js" in reply


def test_report_summary_mentions_wrong_manual_claims(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    reply = chat.handle("show report")
    assert "Where the manual or code was wrong" in reply and "t-9" in reply


# ---------- T10.3 background run ----------
def test_start_acknowledges_immediately_and_reports_progress(tmp_path, run_dir):
    gate = threading.Event()

    def runner(module, run_id, runs_dir):
        gate.wait(5)
        return StageResult(0, "done")

    chat, emitted = make(tmp_path, run_dir, runner=runner)
    ack = chat.handle("start")
    assert ack.startswith("Started run")           # returned while the first stage is still blocked
    assert not gate.is_set() and chat.runner.running()
    assert "Run r-t is going" in chat.handle("status")
    gate.set()
    chat.runner.join(10)
    assert emitted[-1].startswith("Run r-t finished")
    assert sum(m.startswith("Finished") or m.startswith("Rebuild") for m in emitted) == len(STAGES)
    assert "finished" in chat.handle("status")


def test_second_start_while_running_does_not_start_twice(tmp_path, run_dir):
    gate = threading.Event()
    calls = []

    def runner(module, run_id, runs_dir):
        calls.append(module)
        gate.wait(5)
        return StageResult(0)

    chat, _ = make(tmp_path, run_dir, runner=runner)
    chat.handle("start")
    assert "already going" in chat.handle("start")
    gate.set()
    chat.runner.join(10)
    assert len(calls) == len(STAGES)


def test_stage_failure_is_retried_then_stops_and_resumes(tmp_path, run_dir):
    attempts = {"hypothesizer.main.hypothesize": 0}
    fail = {"on": True}

    def runner(module, run_id, runs_dir):
        if module == "hypothesizer.main.hypothesize":
            attempts[module] += 1
            if fail["on"]:
                return StageResult(1, "model overloaded")
        return StageResult(0)

    chat, emitted = make(tmp_path, run_dir, runner=runner)
    chat.handle("start")
    chat.runner.join(10)
    assert attempts[module := "hypothesizer.main.hypothesize"] == 2     # one retry
    assert emitted[-1].startswith("Stopped at hypothesize")
    state = RunState.load(run_dir, run_dir.name)
    assert state.completed == ["survey", "audit"] and state.failed == "hypothesize"
    assert "stopped at hypothesize" in chat.handle("status")

    fail["on"] = False
    seen = []
    chat.runner._stage_runner = lambda m, r, d: (seen.append(m), StageResult(0))[1]
    ack = chat.handle("start")
    chat.runner.join(10)
    assert "Resuming after survey, audit" in ack
    assert seen[0] == "hypothesizer.main.hypothesize" and len(seen) == 2    # survey, audit skipped
    assert RunState.load(run_dir, run_dir.name).finished


def test_blocked_rebuild_is_an_outcome_not_a_crash(tmp_path, run_dir):
    def runner(module, run_id, runs_dir):
        return StageResult(1 if module == "rebuilder.main.cli" else 0)
    chat, emitted = make(tmp_path, run_dir, runner=runner)
    chat.handle("start")
    chat.runner.join(10)
    assert any("blocked" in m for m in emitted) and emitted[-1].startswith("Run r-t finished")


# ---------- T10.5 reliability ----------
def test_with_retry_gives_up_on_non_retryable():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("bad")
    with pytest.raises(ValueError):
        with_retry(fn, attempts=3, sleep=lambda s: None, retry_on=lambda e: False)
    assert len(calls) == 1


def test_corrupt_ledger_falls_back_to_last_good_copy(tmp_path, run_dir):
    chat, _ = make(tmp_path, run_dir)
    assert "3 claims" in chat.handle("ledger")                 # first read snapshots a good copy
    with (run_dir / "ledger.jsonl").open("a") as f:
        f.write("{not json\n")
    reply = chat.handle("ledger")
    assert "last good copy" in reply and "3 claims" in reply
    view = load_ledger(run_dir)
    assert view.degraded and view.ledger is not None


def test_corrupt_ledger_without_snapshot_is_reported_not_crashed(tmp_path):
    d = tmp_path / "r-bad"
    d.mkdir()
    (d / "ledger.jsonl").write_text("{broken\n")
    chat = Coordinator(tmp_path, "r-bad", lambda m: None)
    assert "can't read the ledger" in chat.handle("ledger")


def test_corrupt_state_file_restarts_stage_list(tmp_path):
    d = tmp_path / "r-s"
    d.mkdir()
    RunState.path(d).write_text("{nope")
    assert RunState.load(d, "r-s").completed == []
