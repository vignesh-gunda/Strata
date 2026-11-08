"""Tests for the live view server (static files, ledger, event stream)."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from ui.server import make_server, read_ledger


@pytest.fixture()
def base_url(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"claims": [
        {"id": "c-1", "claim": "vent lowers pressure", "status": "confirmed", "evidence": ["t-000003"]},
        {"id": "c-2", "claim": "bogus", "status": "certain"},
    ]}))
    server = make_server(0, ledger, tmp_path / "traces")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.headers.get("Content-Type"), response.read()


def read_events(url, count):
    events = []
    with urllib.request.urlopen(url, timeout=10) as response:
        name = None
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
                if len(events) >= count:
                    break
    return events


def test_page_and_assets_are_served(base_url):
    status, kind, body = get(base_url + "/")
    assert status == 200 and kind.startswith("text/html") and b"STRATA" in body
    assert get(base_url + "/static/app.js")[0] == 200


def test_bundled_art_is_served_offline(base_url):
    for path in ("/static/assets/cards/2699.svg", "/static/assets/faces/player-4.svg"):
        status, kind, body = get(base_url + path)
        assert status == 200 and kind == "image/svg+xml" and body.lstrip().startswith(b"<svg")
    with pytest.raises(urllib.error.HTTPError):
        get(base_url + "/static/assets/../server.py")


def test_unknown_and_traversal_paths_are_404(base_url):
    for path in ("/static/../server.py", "/static/secret.txt", "/nope"):
        with pytest.raises(urllib.error.HTTPError) as error:
            get(base_url + path)
        assert error.value.code == 404


def test_ledger_keeps_only_valid_statuses(base_url, tmp_path):
    _, _, body = get(base_url + "/api/ledger")
    ledger = json.loads(body)
    assert ledger["connected"] is True
    assert [claim["id"] for claim in ledger["claims"]] == ["c-1"]
    assert read_ledger(tmp_path / "missing.json") == {"connected": False, "claims": []}


def test_stream_sends_run_game_and_trace_events(base_url, tmp_path):
    events = read_events(base_url + "/api/stream?seed=7&mutations=1&delay=0&games=2", 6)
    names = [name for name, _ in events]
    assert names[:3] == ["run", "game", "trace"]
    run, game, trace = events[0][1], events[1][1], events[2][1]
    assert game["label"].startswith("scenario:") and game["view"]["players"] == game["players"]
    assert trace["trace_id"] == "t-000001" and trace["game_id"] == game["game_id"]
    assert run["run_id"].startswith("ui-7-")


def test_stream_rejects_bad_parameters(base_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(base_url + "/api/stream?delay=99")
    assert error.value.code == 400


# ---------- the agent's run folder: ledger and live feed ----------
@pytest.fixture()
def agent_url(tmp_path):
    from livefeed.main.events import EventLog
    from ledger.main.models import Source, Status
    from ledger.main.store import Ledger

    run = tmp_path / "r-ui"
    ledger = Ledger(run / "ledger.jsonl")
    claim = ledger.add_claim(text="Hand limit is 7", source=Source.MODEL, created_by="t")
    ledger.set_status(claim.id, Status.INFERRED, ["t-000001"])
    log = EventLog(run)
    log.emit("survey", {"claims": 3, "unknowns": 1})
    log.emit("score", {"outcome": "refuted", "text": "Vent lowers pressure by 3"}, trace_ids=["t-000412"])
    server = make_server(0, None, tmp_path / "traces", run)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", log
    server.shutdown()
    server.server_close()


def test_ledger_comes_from_the_agent_run_folder(agent_url):
    url, _ = agent_url
    ledger = json.loads(get(url + "/api/ledger")[2])
    assert ledger["connected"] and ledger["claims"] == [
        {"id": "c-0001", "claim": "Hand limit is 7", "status": "inferred", "evidence": ["t-000001"],
         "experiment": None}]


def test_feed_sends_one_event_per_loop_with_the_loop_as_its_id(agent_url):
    url, _ = agent_url
    with urllib.request.urlopen(url + "/api/feed?follow=0", timeout=10) as response:
        body = response.read().decode()
    assert "event: status" in body and '"connected":true' in body
    assert "id: 1\nevent: feed" in body and "id: 2\nevent: feed" in body
    last = json.loads(body.strip().split("data: ")[-1])
    assert last["line"] == "[loop 2] Marked 'Vent lowers pressure by 3' as wrong (trace t-000412)."
    assert last["trace_ids"] == ["t-000412"]


def test_feed_resumes_after_the_last_event_id(agent_url):
    url, _ = agent_url
    request = urllib.request.Request(url + "/api/feed?follow=0", headers={"Last-Event-ID": "1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode()
    assert "id: 1\n" not in body and "id: 2\n" in body


def test_feed_pushes_new_loops_while_connected(agent_url):
    url, log = agent_url
    threading.Timer(0.5, lambda: log.emit("audit", {"refuted": 1})).start()
    events = read_events(url + "/api/feed?since=2", 2)       # the status, then the loop written later
    assert events[0][0] == "status"
    assert events[1][0] == "feed" and events[1][1]["loop"] == 3 and events[1][1]["type"] == "audit"


def test_feed_without_a_run_says_it_is_not_connected(base_url):
    events = read_events(base_url + "/api/feed", 1)
    assert events == [("status", {"connected": False, "run": None})]


def test_feed_rejects_a_bad_since(agent_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(agent_url[0] + "/api/feed?since=abc")
    assert error.value.code == 400


def test_feed_replay_resends_the_saved_run_in_order_with_next_lines(agent_url):
    url, _ = agent_url
    with urllib.request.urlopen(url + "/api/feed?replay=0", timeout=10) as response:
        body = response.read().decode()
    loops = [json.loads(chunk.split("data: ")[1]) for chunk in body.split("\n\n") if chunk.startswith("id:")]
    assert [e["loop"] for e in loops] == [1, 2]
    assert loops[0]["next"].startswith("Next: ") and loops[0]["line"].startswith("[loop 1]")


def test_feed_rejects_a_bad_replay_speed(agent_url):
    with pytest.raises(urllib.error.HTTPError) as error:
        get(agent_url[0] + "/api/feed?replay=fast")
    assert error.value.code == 400
