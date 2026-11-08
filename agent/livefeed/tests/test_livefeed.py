"""T15.1 to T15.3 tests: event log, sentence templates, live stream (chat, terminal)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from coordinator.main.chat import Coordinator
from coordinator.main.runs import StageResult
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from livefeed.main.events import EVENT_TYPES, Event, EventLog, events_path, read_events, safe_emit
from livefeed.main.export import ui_event, ui_ledger
from livefeed.main.lines import MAX_LINE, TEMPLATES, line, sentence
from livefeed.main.stream import FeedWatcher, follow
from livefeed.main.stream import main as tail_main

ONE_STEP = {"action": {"type": "draw"}, "view": None}

SAMPLES = {
    "survey": {"claims": 14, "unknowns": 6, "probes": 9},
    "audit": {"refuted": 4, "supported": 60, "undecided": 25},
    "probe": {"title": "jam then draw", "steps": 12, "surprises": 2, "predicted": True},
    "hypothesize": {"group": "pressure", "round": 2, "proposed": 3},
    "score": {"outcome": "confirmed", "text": "Hand limit is 7", "heldout": 8},
    "rebuild": {"language": "Go", "runnable": True, "unknown": 3},
    "verify": {"verdict": "Accepted", "games": 5000},
    "repair": {"attempt": 2},
}


def ev(type, data=None, loop=1, ids=()):
    return Event(loop=loop, at="2026-11-07T00:00:00Z", type=type, data=data or {}, trace_ids=list(ids))


# ---------- T15.1 event log ----------
def test_events_are_appended_in_order_with_loop_numbers(tmp_path):
    log = EventLog(tmp_path)
    a = log.emit("survey", {"claims": 3})
    b = log.emit("probe", {"steps": 4}, trace_ids=["t-1", "t-2"])
    assert (a.loop, b.loop) == (1, 2)
    rows = [json.loads(x) for x in events_path(tmp_path).read_text().splitlines()]
    assert [r["type"] for r in rows] == ["survey", "probe"]
    assert rows[1]["trace_ids"] == ["t-1", "t-2"] and rows[1]["at"] and rows[1]["loop"] == 2


def test_loop_numbers_continue_after_a_restart_and_a_second_writer(tmp_path):
    EventLog(tmp_path).emit("survey")
    assert EventLog(tmp_path).emit("audit").loop == 2


def test_unknown_event_type_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        EventLog(tmp_path).emit("daydream")


def test_concurrent_writers_never_duplicate_a_loop(tmp_path):
    def work():
        log = EventLog(tmp_path)
        for _ in range(15):
            log.emit("probe", {"steps": 1})
    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    loops = [e.loop for e in read_events(tmp_path)]
    assert loops == list(range(1, 61))


def test_torn_last_line_is_skipped_and_numbering_survives(tmp_path):
    log = EventLog(tmp_path)
    log.emit("survey")
    with events_path(tmp_path).open("a") as f:
        f.write('{"loop": 2, "at"')                     # a crash mid-write
    assert [e.loop for e in read_events(tmp_path)] == [1]
    assert log.emit("audit").loop == 2


def test_restricted_text_is_never_stored(tmp_path):
    event = EventLog(tmp_path).emit("score", {"text": "see services/pressure/sealed"}, trace_ids=["t-1"])
    assert event.data == {"redacted": True} and event.trace_ids == []
    assert "services/" not in events_path(tmp_path).read_text()


def test_safe_emit_never_raises(tmp_path):
    safe_emit(None, "survey")
    safe_emit(EventLog(tmp_path), "not-a-type")         # swallowed
    assert read_events(tmp_path) == []


# ---------- T15.2 line writer ----------
def test_every_event_type_has_a_template_and_a_sample():
    assert set(TEMPLATES) == set(EVENT_TYPES) == set(SAMPLES)


@pytest.mark.parametrize("type", EVENT_TYPES)
def test_each_line_is_one_short_sentence(type):
    text = line(ev(type, SAMPLES[type], loop=7, ids=["t-000412"]))
    assert text.startswith("[loop 7] ") and "\n" not in text and len(text) <= MAX_LINE
    body = sentence(ev(type, SAMPLES[type]))
    assert body.endswith(".") and body.count(". ") == 0 and body[0].isupper()


def test_score_lines_match_the_documented_examples():
    assert line(ev("score", {"outcome": "refuted", "text": "Vent lowers pressure by 3"}, 5, ["t-000412"])) == \
        "[loop 5] Marked 'Vent lowers pressure by 3' as wrong (trace t-000412)."
    assert "Confirmed 'Hand limit is 7' on 8 held-out steps" in line(ev("score", SAMPLES["score"], 6, ["t-9"]))


def test_free_text_cannot_add_a_second_sentence_or_a_line_break():
    nasty = "Vent. Then everything else.\nSecond line! And more? yes"
    text = line(ev("score", {"outcome": "refuted", "text": nasty}))
    assert "\n" not in text and text.count(". ") == 0 and text.endswith(".")


def test_long_text_is_cut_to_the_line_limit():
    text = line(ev("score", {"outcome": "refuted", "text": "x" * 500}))
    assert len(text) <= MAX_LINE


def test_missing_or_wrong_data_still_gives_a_line():
    for type in EVENT_TYPES:
        assert line(ev(type, {})).startswith("[loop 1] ")
        assert line(ev(type, {"steps": "many", "claims": None, "heldout": [1]})).endswith(".")
    assert "Did something" in sentence(ev("mystery"))


def test_forbidden_text_in_an_event_is_blocked_at_the_line_writer():
    text = line(ev("probe", {"title": "read services/pressure/sealed now"}))
    assert "services" not in text and "restricted" in text
    assert "restricted" in line(ev("survey", {"redacted": True}))


def test_probe_line_reports_surprises_honestly():
    assert "2 moves did not match" in sentence(ev("probe", SAMPLES["probe"]))
    quiet = {**SAMPLES["probe"], "surprises": 0}
    assert "everything matched" in sentence(ev("probe", quiet))
    assert "no guesses to compare yet" in sentence(ev("probe", {**quiet, "predicted": False}))


def test_verify_blocked_line_has_the_reason():
    text = sentence(ev("verify", {"verdict": "Blocked", "games": 12, "reason": "trace 4 differs"}))
    assert "blocked (trace 4 differs)" in text


# ---------- T15.3 live stream ----------
def test_watcher_posts_each_new_loop_once_in_order(tmp_path):
    log, got = EventLog(tmp_path), []
    log.emit("survey", SAMPLES["survey"])                   # already there: not replayed
    watcher = FeedWatcher(tmp_path, got.append, poll=0.05).start()
    log.emit("audit", SAMPLES["audit"])
    log.emit("probe", SAMPLES["probe"])
    deadline = time.time() + 3
    while len(got) < 4 and time.time() < deadline:
        time.sleep(0.02)
    watcher.stop()
    assert [g.split("]")[0] for g in got if g.startswith("[loop")] == ["[loop 2", "[loop 3"]
    assert [g.strip().startswith("Next:") for g in got] == [False, True, False, True]
    assert watcher.drain() == 0                              # nothing is sent twice


def test_watcher_from_start_replays_everything(tmp_path):
    log = EventLog(tmp_path)
    log.emit("survey", SAMPLES["survey"])
    log.emit("audit", SAMPLES["audit"])
    got = []
    assert FeedWatcher(tmp_path, got.append, from_start=True).drain() == 2


def test_follow_yields_new_events_and_stops(tmp_path):
    log = EventLog(tmp_path)
    log.emit("survey")
    threading.Timer(0.15, lambda: log.emit("audit")).start()
    seen, state = [], {"done": False}
    for event in follow(tmp_path, 0, poll=0.05, stop=lambda: state["done"]):
        seen.append(event.loop)
        if len(seen) == 2:
            state["done"] = True
    assert seen == [1, 2]


def test_terminal_tail_prints_the_same_lines(tmp_path, capsys):
    run = tmp_path / "r-x"
    log = EventLog(run)
    for type in ("survey", "audit", "probe"):
        log.emit(type, SAMPLES[type])
    assert tail_main(["--run-id", "r-x", "--runs-dir", str(tmp_path), "--last", "2"]) == 0
    out = [o for o in capsys.readouterr().out.splitlines() if o.startswith("[loop")]
    assert [o.split("]")[0] for o in out] == ["[loop 2", "[loop 3"]
    assert out[0] == line(read_events(run)[1])


def test_terminal_tail_with_no_events_says_so(tmp_path, capsys):
    assert tail_main(["--run-id", "none", "--runs-dir", str(tmp_path)]) == 0
    assert "No events yet" in capsys.readouterr().err


def test_chat_receives_feed_lines_before_the_stage_summary(tmp_path):
    run_dir = tmp_path / "r-c"
    Ledger(run_dir / "ledger.jsonl").add_claim(text="A claim", source=Source.MANUAL, created_by="t")
    posted = []

    def stage(module, run_id, runs_dir, *extra):                   # a stage that logs events like the real ones
        log = EventLog(Path(runs_dir) / run_id)
        if module.endswith("survey"):
            log.emit("survey", SAMPLES["survey"])
        if module.endswith("audit"):
            log.emit("audit", SAMPLES["audit"])
            log.emit("score", {"outcome": "refuted", "text": "Vent lowers pressure by 3"}, trace_ids=["t-000412"])
        return StageResult(0)

    chat = Coordinator(tmp_path, "r-c", posted.append, stage_runner=stage, sleep=lambda s: None)
    chat.handle("start")
    chat.runner.join(10)
    loops = [p for p in posted if p.startswith("[loop ")]
    assert [p.split("]")[0] for p in loops] == ["[loop 1", "[loop 2", "[loop 3"]
    assert posted.index(loops[0]) < next(i for i, p in enumerate(posted) if p.startswith("Finished survey"))
    assert posted.index(loops[2]) < next(i for i, p in enumerate(posted) if p.startswith("Finished audit"))
    assert "trace t-000412" in loops[2]


# ---------- the UI contract ----------
def test_ui_ledger_has_the_shape_the_page_expects(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    claim = ledger.add_claim(text="Hand limit is 7", source=Source.MODEL, created_by="t")
    ledger.set_status(claim.id, Status.INFERRED, ["t-1"])
    ledger.link_experiment(claim.id, "exp-1")
    result = ui_ledger(tmp_path)
    assert result["connected"] is True
    assert result["claims"] == [{"id": claim.id, "claim": "Hand limit is 7", "status": "inferred",
                                 "evidence": ["t-1"], "experiment": "exp-1"}]
    assert not (tmp_path / "ledger.good.jsonl").exists()      # polling never copies the file


def test_ui_ledger_without_a_run_is_not_connected(tmp_path):
    assert ui_ledger(tmp_path / "nope") == {"connected": False, "claims": []}


def test_ui_event_carries_line_sentence_and_traces():
    out = ui_event(ev("score", {"outcome": "refuted", "text": "X"}, 3, ["t-1"]))
    assert out["loop"] == 3 and out["line"].startswith("[loop 3] ") and out["trace_ids"] == ["t-1"]
    assert not out["sentence"].startswith("[loop")


# ---------- the pipeline really writes the events ----------
def test_scorer_logs_a_refutation_with_its_trace_ids(tmp_path):
    from auditor.main.corpus import TraceStore
    from auditor.main.synthetic import make_game, make_view
    from scorer.main.scorer import Scorer

    store = TraceStore([make_game("g-1", [{**ONE_STEP, "view": make_view()}])])
    ledger = Ledger(trace_validator=store.exists)
    claim = ledger.add_claim(text="Vent lowers pressure by 3", source=Source.MANUAL, created_by="t")
    log = EventLog(tmp_path)
    Scorer(ledger, store, events=log).refute(claim.id, [store.games()[0].steps[0]["trace_id"]], "stale")
    [event] = read_events(tmp_path)
    assert event.type == "score" and event.data["outcome"] == "refuted" and event.trace_ids
    assert line(event).endswith(f"as wrong (trace {event.trace_ids[0]}).")


def test_scorer_without_a_log_behaves_as_before(tmp_path):
    from auditor.main.corpus import TraceStore
    from auditor.main.synthetic import make_game, make_view
    from scorer.main.scorer import Scorer

    store = TraceStore([make_game("g-1", [{**ONE_STEP, "view": make_view()}])])
    ledger = Ledger(trace_validator=store.exists)
    claim = ledger.add_claim(text="x", source=Source.MANUAL, created_by="t")
    Scorer(ledger, store).refute(claim.id, [store.games()[0].steps[0]["trace_id"]], "stale")
    assert ledger.get(claim.id).status is Status.REFUTED


def test_runner_logs_one_probe_event_per_game(tmp_path):
    from prober.tests.test_prober import runner_for
    log = EventLog(tmp_path)
    runner, _, _, _ = runner_for(events=log)
    game = runner.run_random(seed=1, players=2)
    [event] = read_events(tmp_path)
    assert event.type == "probe" and event.data["steps"] == 3
    assert event.trace_ids == [game.steps[0]["trace_id"], game.steps[-1]["trace_id"]]


def test_rebuild_loop_logs_rebuild_verify_repair_in_order(tmp_path):
    from rebuilder.tests.test_rebuilder import BUGGY, GOOD, RULES, fenced, verify_against_toy, world
    from rebuilder.main.rebuild import build_and_repair
    rebuilder, _, _ = world(fenced(BUGGY), fenced(GOOD))
    outcome = build_and_repair(rebuilder, RULES, tmp_path / "rb", verify_against_toy, max_attempts=3,
                               events=EventLog(tmp_path))
    assert outcome.accepted
    events = read_events(tmp_path)
    assert [e.type for e in events] == ["rebuild", "verify", "repair", "rebuild", "verify"]
    assert [e.data.get("verdict") for e in events if e.type == "verify"] == ["Blocked", "Accepted"]
    assert all(line(e).endswith(".") for e in events)


# ---------- T15.4 next-step line ----------
def test_next_line_follows_the_loop_state():
    from livefeed.main.next_step import next_line
    first = ev("survey", SAMPLES["survey"], 1)
    assert next_line([first]) == "Next: check those claims against the recorded games."
    audit = ev("audit", SAMPLES["audit"], 2)
    assert next_line([first, audit]) == "Next: test 'Draw until the hand is full'."
    probe = ev("probe", {**SAMPLES["probe"], "experiment": "e-draw-limit"}, 3)
    assert next_line([first, audit, probe]) == "Next: test 'Jam a seat, then make it draw and play'."


def test_next_line_moves_on_when_every_seed_experiment_is_done():
    from livefeed.main.next_step import next_line
    from prober.main.experiments import seed_experiments
    done = [ev("probe", {"experiment": x.id, "steps": 5}, n + 1) for n, x in enumerate(seed_experiments())]
    assert next_line(done) == "Next: ask for rules that explain what was seen."


def test_next_line_after_verify_depends_on_the_verdict():
    from livefeed.main.next_step import next_line
    assert next_line([ev("verify", {"verdict": "Accepted"})]) == "Next: write up the results."
    assert "fix the rebuild" in next_line([ev("verify", {"verdict": "Blocked"})])
    assert "does not run" in next_line([ev("rebuild", {"runnable": False})])
    assert next_line([]) is None and next_line([ev("survey", {"redacted": True})]) is None


def test_next_line_is_one_short_line_for_every_event_type():
    from livefeed.main.next_step import next_line
    for type in EVENT_TYPES:
        text = next_line([ev(type, SAMPLES[type])])
        assert text and text.startswith("Next: ") and "\n" not in text and text.endswith(".")


def test_ui_event_carries_the_next_line():
    out = ui_event(ev("survey", SAMPLES["survey"]), [ev("survey", SAMPLES["survey"])])
    assert out["next"].startswith("Next: ")
    assert ui_event(ev("survey", SAMPLES["survey"]))["next"] is None


# ---------- T15.5 safe by default ----------
def test_api_keys_never_reach_a_line_or_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "abcd1234efgh5678")
    event = EventLog(tmp_path).emit("score", {"outcome": "refuted", "text": "key abcd1234efgh5678 here"})
    assert event.data == {"redacted": True}
    assert "abcd1234" not in events_path(tmp_path).read_text()
    leaky = ev("score", {"outcome": "refuted", "text": "see abcd1234efgh5678"})
    assert "abcd1234" not in line(leaky) and "restricted" in line(leaky)


@pytest.mark.parametrize("secret", ["sk-abcdefghijklmnop1234", "Bearer abcdefghijklmnopqrst",
                                    "a" * 40, "A1b2" * 14])
def test_key_shaped_text_is_blocked(secret):
    assert "restricted" in line(ev("score", {"outcome": "refuted", "text": secret}))


@pytest.mark.parametrize("raw", ["```python\ndef predict(): pass\n```", '{"rule": "x", "code": "y"}',
                                 "def predict(view, action): return None", "word " * 70])
def test_raw_model_output_is_never_quoted(raw):
    text = line(ev("score", {"outcome": "confirmed", "text": raw}, 4))
    assert "def predict" not in text and "```" not in text and "{" not in text and "restricted" in text


def test_ordinary_claim_text_is_not_mistaken_for_raw_output():
    text = line(ev("score", {"outcome": "refuted", "text": "A player may hold up to 7 cards"}, 2, ["t-1"]))
    assert "hold up to 7 cards" in text and "restricted" not in text


# ---------- T15.6 replay ----------
def _timed_run(tmp_path):
    path = events_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamps = ["2026-11-07T10:00:00.000+00:00", "2026-11-07T10:00:01.000+00:00", "2026-11-07T10:10:00.000+00:00"]
    rows = [ev("survey", SAMPLES["survey"], 1), ev("audit", SAMPLES["audit"], 2),
            ev("probe", {**SAMPLES["probe"], "experiment": "e-draw-limit"}, 3)]
    with path.open("w") as f:
        for row, at in zip(rows, stamps):
            f.write(json.dumps({**row.model_dump(), "at": at}) + "\n")
    return tmp_path


def test_replay_gives_the_same_lines_in_the_same_order_as_live(tmp_path):
    from livefeed.main.stream import lines_for, replay_events
    run = _timed_run(tmp_path)
    live = [t for e in read_events(run) for t in lines_for([x for x in read_events(run) if x.loop <= e.loop], e)]
    replayed = [t for e, h in replay_events(run, 0) for t in lines_for(h, e)]
    again = [t for e, h in replay_events(run, 50, sleep=lambda s: None) for t in lines_for(h, e)]
    assert replayed == live == again and len(live) == 6


def test_replay_paces_by_recorded_time_divided_by_speed_and_caps_long_pauses(tmp_path):
    from livefeed.main.stream import replay_events
    run, waits = _timed_run(tmp_path), []
    list(replay_events(run, 4, sleep=waits.append))
    assert waits == [0.25, 0.5]                   # 1s/4, then the 10 minute pause capped at 2s, /4
    waits.clear()
    list(replay_events(run, 0, sleep=waits.append))
    assert waits == []                            # speed 0 never waits


def test_replay_from_the_terminal(tmp_path, capsys):
    run = _timed_run(tmp_path / "r-r")
    assert tail_main(["--run-id", "r-r", "--runs-dir", str(tmp_path), "--replay", "0"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [o.split("]")[0] for o in out if o.startswith("[loop")] == ["[loop 1", "[loop 2", "[loop 3"]


def test_replay_with_no_events_says_so(tmp_path, capsys):
    assert tail_main(["--run-id", "none", "--runs-dir", str(tmp_path), "--replay", "0"]) == 0
    assert "No events to replay" in capsys.readouterr().err
