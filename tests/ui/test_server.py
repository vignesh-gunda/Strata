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
