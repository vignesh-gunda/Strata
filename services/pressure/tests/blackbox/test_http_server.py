import json
import threading
import urllib.error
import urllib.request

import pytest

from blackbox.api import BlackBox
from blackbox.http_server import MAX_BODY, PressureService
from blackbox.scripts import script_from_seed, stacked_script
from blackbox.server import handle_line


@pytest.fixture
def service():
    def start(box: BlackBox, allow_reveal: bool = False):
        server = PressureService(("127.0.0.1", 0), box, allow_reveal)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    started = []
    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_health(service):
    base = service(BlackBox())
    assert call(base, "GET", "/health") == (200, {"ok": True, "result": {"service": "pressure", "version": "1.0"}})


def test_full_game_flow(service):
    base = service(BlackBox())
    status, game = call(base, "POST", "/v1/games", {"players": 2, "script": script_from_seed(3)})
    assert status == 200 and game["ok"]
    game_id = game["result"]["game_id"]
    status, legal = call(base, "GET", f"/v1/games/{game_id}/legal-actions")
    assert status == 200 and legal["result"]
    status, acted = call(base, "POST", f"/v1/games/{game_id}/actions", legal["result"][0])
    assert status == 200 and "events" in acted["result"]
    status, view = call(base, "GET", f"/v1/games/{game_id}/view")
    assert status == 200 and view["result"] == acted["result"]["view"]


def test_http_matches_json_lines_for_the_same_game():
    """Both transports share one dispatcher, so the same requests give the same results."""
    script = stacked_script([["gear3", "vent", "jam", "scan"], ["gear2", "gear1", "double", "reverse"]], ["gear1"] * 6)
    over_http, over_lines = BlackBox(), BlackBox()
    service = PressureService(("127.0.0.1", 0), over_http)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{service.server_address[1]}"
        http_game = call(base, "POST", "/v1/games", {"players": 2, "script": script})[1]["result"]
        line_game = handle_line(over_lines, json.dumps({"method": "new_game", "params": {"players": 2, "script": script}}))
        assert http_game == line_game["result"]
        for action in ({"type": "play", "card": "gear3"}, {"type": "play", "card": "gear2"}):
            http_result = call(base, "POST", "/v1/games/g-0001/actions", action)[1]["result"]
            line_result = handle_line(over_lines, json.dumps(
                {"method": "act", "params": {"game_id": "g-0001", "action": action}}))["result"]
            assert http_result == line_result
    finally:
        service.shutdown()
        service.server_close()


@pytest.mark.parametrize(
    ("method", "path", "body", "status", "kind"),
    [
        ("GET", "/v1/nowhere", None, 404, "not_found"),
        ("GET", "/v1/games", None, 405, "method_not_allowed"),
        ("GET", "/v1/games/g-9999/view", None, 404, "game_not_found"),
        ("POST", "/v1/games", {"players": 9, "script": {"deck": ["gear1"]}}, 400, "invalid_request"),
        ("GET", "/v1/reveal", None, 403, "forbidden"),
        ("DELETE", "/v1/games", None, 405, "method_not_allowed"),
    ],
)
def test_errors_use_the_envelope_and_status(service, method, path, body, status, kind):
    base = service(BlackBox())
    code, response = call(base, method, path, body)
    assert code == status
    assert response["ok"] is False and response["error"]["kind"] == kind


def test_illegal_action_is_409(service):
    base = service(BlackBox())
    game_id = call(base, "POST", "/v1/games", {"players": 2, "script": script_from_seed(1)})[1]["result"]["game_id"]
    code, response = call(base, "POST", f"/v1/games/{game_id}/actions", {"type": "pass"})
    assert code == 409 and response["error"]["kind"] == "illegal_action"


def test_bad_json_and_oversized_body(service):
    import http.client
    from urllib.parse import urlparse

    base = service(BlackBox())
    assert call(base, "POST", "/v1/games", raw=b"{not json")[0] == 400
    address = urlparse(base)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
    connection.putrequest("POST", "/v1/games")
    connection.putheader("Content-Length", str(MAX_BODY + 1))  # rejected from the header, before any body is read
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413 and json.load(response)["error"]["kind"] == "payload_too_large"
    connection.close()


def test_reveal_needs_the_flag(service):
    base = service(BlackBox.from_seed(42, 2), allow_reveal=True)
    code, response = call(base, "GET", "/v1/reveal")
    assert code == 200 and len(response["result"]["mutations"]) == 2
