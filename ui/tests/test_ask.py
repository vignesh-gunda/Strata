"""T16.11: the ask box, the agent trace stream and the live file panel."""

import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from ui.server import make_server  # noqa: F401  (also puts the agent packages on sys.path)
from ui.agent_bridge import (ASKS_PER_WINDOW, MAX_ASK_CHARS, AgentBridge, CancellableStageRunner)
from ui.agent_traces import GameTail, game_events
from ui.project_files import FileJail, PathRefused, diff_fingerprints


# ---------- helpers ----------
def step(n, winner=None):
    return {"trace_id": f"x-{n:06d}", "run_id": "r", "game_id": "px-0001", "step": n, "seat": 0, "turn": n,
            "action": {"type": "draw"}, "legal": [{"type": "draw"}], "events": [],
            "view": {"players": 2, "hands": [[], []], "alive": [True, True], "discard": [], "draw_pile_size": 3,
                     "current_seat": 0, "actions_left": 1, "pressure": 0, "turn": n, "over": winner is not None,
                     "winner": winner},
            "split": "train", "label": "probe", "prediction": None, "surprise": None}


def game(n):
    return {"game_id": f"px-{n:04d}", "players": 2, "split": "train", "label": "experiment:e-test",
            "initial_view": {"players": 2, "hands": [[], []], "winner": None},
            "steps": [step(1), step(2, winner=1)]}


class FakeBridge:
    def __init__(self):
        self.asked, self.cancelled = [], 0

    def ask(self, text):
        self.asked.append(text)
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "reply": "Type a question or a request first.", "running": False}
        return {"ok": True, "reply": f"echo: {text}", "running": False}

    def cancel(self):
        self.cancelled += 1
        return {"ok": True, "cancelled": True, "reply": "Stopping the run."}

    def status(self):
        return {"enabled": True, "running": False, "status": "ready", "models": {}, "budget": {}}


@pytest.fixture()
def run_dir(tmp_path):
    folder = tmp_path / "runs" / "r-test"
    (folder / "rebuild" / "go").mkdir(parents=True)
    (folder / "rebuild" / "go" / "engine.go").write_text("package main\n")
    (folder / "probe_games.jsonl").write_text("".join(json.dumps(game(n)) + "\n" for n in (1, 2, 3)))
    return folder


@pytest.fixture()
def server(run_dir, tmp_path):
    bridge = FakeBridge()
    srv = make_server(0, None, tmp_path / "traces", run_dir, bridge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, bridge
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None, host=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, raw


def sse(raw):
    events, name = [], None
    for line in raw.decode().splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            events.append((name, json.loads(line.removeprefix("data: "))))
    return events


# ---------- T16.1 / T16.2: the ask endpoint ----------
def test_ask_returns_the_reply(server):
    srv, bridge = server
    status, raw = request(srv, "POST", "/api/ask", {"text": "explain the rules"})
    assert status == 200 and json.loads(raw)["reply"] == "echo: explain the rules"
    assert bridge.asked == ["explain the rules"]


def test_ask_rejects_bad_requests(server):
    srv, _ = server
    assert request(srv, "POST", "/api/ask", b"not json")[0] == 400
    assert request(srv, "POST", "/api/ask", [1, 2])[0] == 400
    assert request(srv, "POST", "/api/ask", b"x" * 5000)[0] == 413
    assert request(srv, "POST", "/api/ask", b"text=hi", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert request(srv, "POST", "/api/ask", {"text": "   "})[0] == 400      # the bridge says "type something"


def test_cancel_calls_the_bridge(server):
    srv, bridge = server
    status, raw = request(srv, "POST", "/api/cancel", {})
    assert status == 200 and json.loads(raw)["cancelled"] is True and bridge.cancelled == 1


def test_ask_without_an_agent_says_so(run_dir, tmp_path):
    srv = make_server(0, None, tmp_path / "traces", run_dir, None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, raw = request(srv, "POST", "/api/ask", {"text": "hi"})
        assert status == 200 and "not attached" in json.loads(raw)["reply"]
        assert json.loads(request(srv, "GET", "/api/agent")[1]) == {"enabled": False}
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- T16.8: local only ----------
def test_foreign_host_and_origin_are_refused(server):
    srv, bridge = server
    assert request(srv, "GET", "/api/agent", host="evil.example")[0] == 403
    assert request(srv, "POST", "/api/ask", {"text": "hi"}, host="evil.example")[0] == 403
    assert request(srv, "POST", "/api/ask", {"text": "hi"}, {"Origin": "http://evil.example"})[0] == 403
    assert bridge.asked == []
    port = srv.server_address[1]
    assert request(srv, "POST", "/api/ask", {"text": "hi"}, {"Origin": f"http://localhost:{port}"})[0] == 200


def test_the_server_binds_to_the_local_machine_only(server):
    srv, _ = server
    assert srv.server_address[0] == "127.0.0.1"


# ---------- T16.3: the agent's own probe games ----------
def test_agent_traces_use_the_table_shape(server):
    srv, _ = server
    status, raw = request(srv, "GET", "/api/agent-traces?follow=0&delay=0")
    names = [name for name, _ in sse(raw)]
    assert status == 200
    assert names[:2] == ["status", "run"]
    assert names.count("game") == 3 and names.count("trace") == 6 and names.count("end") == 3
    first_game = next(data for name, data in sse(raw) if name == "game")
    assert set(first_game) >= {"game_id", "index", "label", "split", "players", "view"}
    end = next(data for name, data in sse(raw) if name == "end")
    assert end == {"game_id": "px-0001", "index": 0, "steps": 2, "winner": 1}


def test_agent_traces_resume_after_a_reconnect(server):
    srv, _ = server
    _, raw = request(srv, "GET", "/api/agent-traces?follow=0&delay=0", headers={"Last-Event-ID": "2"})
    games = [data["game_id"] for name, data in sse(raw) if name == "game"]
    assert games == ["px-0003"]
    ids = [line for line in raw.decode().splitlines() if line.startswith("id: ")]
    assert ids == ["id: 3"]


def test_agent_traces_never_send_restricted_text(run_dir, server):
    srv, _ = server
    bad = game(9)
    bad["label"] = "see services/pressure/sealed/RULES.md"
    with (run_dir / "probe_games.jsonl").open("a") as handle:
        handle.write(json.dumps(bad) + "\n")
    _, raw = request(srv, "GET", "/api/agent-traces?follow=0&delay=0")
    assert b"services/" not in raw and sse(raw)[-1][1]["game_id"] == "px-0003"


def test_a_half_written_line_waits(tmp_path):
    path = tmp_path / "games.jsonl"
    path.write_text(json.dumps(game(1)) + "\n" + json.dumps(game(2))[:30])
    tail = GameTail(path)
    assert [index for index, _ in tail.poll()] == [0]
    with path.open("a") as handle:
        handle.write(json.dumps(game(2))[30:] + "\n")
    assert [index for index, _ in tail.poll()] == [1]
    assert list(tail.poll()) == []


def test_game_events_without_steps():
    events = game_events({"game_id": "px-1", "initial_view": {"winner": None}}, 4)
    assert [name for name, _ in events] == ["game", "end"] and events[-1][1]["steps"] == 0


# ---------- T16.5: the file panel and its jail ----------
def test_the_file_list_and_reader(server):
    srv, _ = server
    files = json.loads(request(srv, "GET", "/api/files")[1])["files"]
    assert [f["path"] for f in files] == ["go/engine.go"]
    status, raw = request(srv, "GET", "/api/files/read?path=go/engine.go")
    assert status == 200 and json.loads(raw)["text"] == "package main\n"


@pytest.mark.parametrize("bad", ["../probe_games.jsonl", "/etc/passwd", "go/../../probe_games.jsonl", ".env",
                                 "go/.hidden", "services/pressure/x", "sealed/RULES.md", "", "a\0b", "go//engine.go"])
def test_the_jail_refuses_escapes_and_secrets(run_dir, bad):
    with pytest.raises(PathRefused):
        FileJail(run_dir / "rebuild").read(bad)


def test_the_jail_refuses_symlinks(run_dir, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    root = run_dir / "rebuild"
    (root / "go" / "link.txt").symlink_to(outside)
    (root / "linkdir").symlink_to(tmp_path)
    jail = FileJail(root)
    with pytest.raises(PathRefused):
        jail.read("go/link.txt")
    with pytest.raises(PathRefused):
        jail.read("linkdir/secret.txt")
    assert [f["path"] for f in jail.list()] == ["go/engine.go"]       # links are not even listed


def test_binary_files_and_restricted_text_are_not_shown(run_dir):
    root = run_dir / "rebuild"
    (root / "go" / "server").write_bytes(b"\x7fELF\0\0\0binary")
    (root / "go" / "notes.txt").write_text("copied from services/pressure/blackbox/rules.py")
    jail = FileJail(root)
    assert jail.read("go/server")["binary"] is True and jail.read("go/server")["text"] is None
    hidden = jail.read("go/notes.txt")
    assert hidden["hidden"] is True and hidden["text"] is None


def test_big_files_are_cut_off(run_dir):
    (run_dir / "rebuild" / "go" / "big.go").write_text("x" * 300_000)
    shown = FileJail(run_dir / "rebuild").read("go/big.go")
    assert shown["truncated"] is True and len(shown["text"]) == 200_000


def test_a_missing_folder_is_just_empty(tmp_path):
    assert FileJail(tmp_path / "nope").list() == []


def test_changes_are_reported():
    before = {"a": (1, 1), "b": (2, 2)}
    after = {"a": (1, 1), "b": (3, 3), "c": (1, 1)}
    assert diff_fingerprints(before, after) == [{"path": "b", "kind": "changed"}, {"path": "c", "kind": "created"}]
    assert diff_fingerprints(after, {"a": (1, 1)}) == [
        {"path": "b", "kind": "deleted"}, {"path": "c", "kind": "deleted"}]


def test_the_file_stream_sends_the_list(server):
    srv, _ = server
    _, raw = request(srv, "GET", "/api/files/stream?follow=0")
    events = sse(raw)
    assert events[0] == ("status", {"connected": True})
    assert [f["path"] for f in events[1][1]["files"]] == ["go/engine.go"]


def test_the_file_stream_reports_a_new_file(run_dir, server):
    srv, _ = server
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/files/stream", headers={"Host": f"127.0.0.1:{port}"})
    response = conn.getresponse()
    seen = []

    def read():
        name = None
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                seen.append((name, json.loads(line.removeprefix("data: "))))

    threading.Thread(target=read, daemon=True).start()
    deadline = time.time() + 5
    while len(seen) < 2 and time.time() < deadline:
        time.sleep(0.05)
    (run_dir / "rebuild" / "go" / "more.go").write_text("package more\n")
    while not any(n == "files" and d["changes"] for n, d in seen) and time.time() < deadline:
        time.sleep(0.05)
    conn.close()
    changed = next(d for n, d in seen if n == "files" and d["changes"])
    assert changed["changes"] == [{"path": "go/more.go", "kind": "created"}]


# ---------- T16.1 / T16.7: the bridge ----------
def make_bridge(tmp_path, **kwargs):
    def no_key():
        raise RuntimeError("no key")

    runs = tmp_path / "runs"
    (runs / "r-test").mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("muse_factory", no_key)
    kwargs.setdefault("subq_factory", no_key)
    return AgentBridge(runs, "r-test", **kwargs)


def test_the_bridge_checks_input(tmp_path):
    bridge = make_bridge(tmp_path)
    assert bridge.ask("")["ok"] is False and bridge.ask(None)["ok"] is False
    long = bridge.ask("x" * (MAX_ASK_CHARS + 1))
    assert long["ok"] is False and str(MAX_ASK_CHARS) in long["reply"]


def test_the_bridge_limits_how_fast_you_can_ask(tmp_path):
    now = [0.0]
    bridge = make_bridge(tmp_path, clock=lambda: now[0])
    replies = [bridge.ask("help") for _ in range(ASKS_PER_WINDOW + 1)]
    assert all(r["ok"] for r in replies[:ASKS_PER_WINDOW]) and replies[-1]["ok"] is False
    now[0] = 61.0
    assert bridge.ask("help")["ok"] is True


def test_a_missing_key_turns_that_model_off_but_still_answers(tmp_path):
    bridge = make_bridge(tmp_path)
    reply = bridge.ask("help")
    assert reply["ok"] is True and "reverse-engineer" in reply["reply"]
    info = bridge.status()
    assert info["models"] == {"muse": False, "subq": False, "started": True}
    assert any("muse is off" in note for note in info["notes"])


def test_status_reports_tokens_used(tmp_path):
    from llm.main.budget import Budget

    class Client:
        budget = Budget()

    Client.budget.record("muse", 120)
    bridge = make_bridge(tmp_path, muse_factory=lambda: Client())
    bridge.ask("help")
    assert bridge.status()["budget"]["muse"]["used"] == 120


def test_only_one_run_at_a_time(tmp_path):
    from coordinator.main.runs import StageResult

    release, started = threading.Event(), threading.Event()

    def slow_stage(module, run_id, runs_dir, *extra):
        started.set()
        release.wait(5)
        return StageResult(0, "")

    bridge = make_bridge(tmp_path, stage_runner=slow_stage, which=lambda name: "/usr/bin/" + name)
    first = bridge.ask("start")
    assert first["ok"] and "Started run" in first["reply"]
    assert started.wait(5)
    second = bridge.ask("start")
    assert "already going" in second["reply"] and bridge.status()["running"] is True
    release.set()
    bridge._coordinator.runner.join(10)
    assert bridge.status()["running"] is False


def test_a_missing_toolchain_is_said_plainly(tmp_path):
    bridge = make_bridge(tmp_path, which=lambda name: None)
    reply = bridge.ask("rebuild it in go")["reply"]
    assert "go" in reply.lower() and "Started run" not in reply


def test_cancel_when_nothing_runs(tmp_path):
    assert make_bridge(tmp_path).cancel() == {"ok": True, "cancelled": False, "reply": "Nothing is running."}


# ---------- T16.7: Cancel stops a stage and keeps the rest ----------
def test_cancel_kills_the_running_stage(tmp_path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "sleeper.py").write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n")
    runner = CancellableStageRunner(agent_root)
    outcome = {}

    def run():
        outcome["result"] = runner("sleeper", "r-test", tmp_path)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.time() + 5
    while runner._proc is None and time.time() < deadline:
        time.sleep(0.05)
    assert runner.cancel() is True
    thread.join(10)
    assert not thread.is_alive() and outcome["result"].code == 130
    again = runner("sleeper", "r-test", tmp_path)          # later attempts do not restart the stage
    assert again.code == 130
    runner.reset()


def test_a_finished_stage_returns_its_last_line(tmp_path):
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "quick.py").write_text("print('one')\nprint('all done')\n")
    result = CancellableStageRunner(agent_root)("quick", "r-test", tmp_path)
    assert result.code == 0 and result.tail == "all done"


def test_cancel_from_the_page_removes_the_code_the_run_was_writing(tmp_path):
    from coordinator.main.runs import StageResult
    from rebuilder.main.languages import GO
    from rebuilder.main.scaffold import folder_for

    gate = threading.Event()

    def stage(module, run_id, runs_dir, *extra):
        gate.wait(5)
        return StageResult(130, "cancelled by you")

    bridge = make_bridge(tmp_path, stage_runner=stage, which=lambda tool: "/usr/bin/" + tool, sleep=lambda s: None)
    assert "rebuilding in Go" in bridge.ask("rebuild it in Go")["reply"]
    folder = folder_for(tmp_path / "runs" / "r-test", GO)
    assert (folder / "program" / "engine.go").exists()
    reply = bridge.cancel()
    assert reply["cancelled"] and "remov" in reply["reply"] and "code" in reply["reply"]
    gate.set()
    bridge._coordinator.runner.join(10)
    assert not folder.exists()
    assert (tmp_path / "runs" / "r-test" / "coordinator_state.json").exists()     # what it learned stays
