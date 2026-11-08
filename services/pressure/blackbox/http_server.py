"""Run the black box as an HTTP microservice.

Same game, same validation and same error kinds as the JSON-lines server;
only the transport differs. Every response uses the same envelope:

    {"ok": true,  "result": {...}}
    {"ok": false, "error": {"kind": "illegal_action", "message": "..."}}

Routes:
    GET  /health
    POST /v1/scripts/random            {"seed": 7}
    POST /v1/games                     {"players": 3, "script": {"deck": [...]}}
    GET  /v1/games/<game_id>/legal-actions
    POST /v1/games/<game_id>/actions   {"type": "play", "card": "gear2"}
    GET  /v1/games/<game_id>/view
    GET  /v1/reveal                    (403 unless started with --allow-reveal)

Usage: python -m blackbox.http_server --seed 42 --mutations 2 [--host 127.0.0.1] [--port 8710]
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from blackbox.api import BlackBox
from blackbox.server import add_box_arguments, build_box, handle_request

SERVICE = "pressure"
VERSION = "1.0"
MAX_BODY = 1_000_000
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8710

_GAME_ROUTE = re.compile(r"^/v1/games/(?P<game_id>[A-Za-z0-9-]{1,32})/(?P<what>legal-actions|actions|view)$")

STATUS_FOR_ERROR = {
    "invalid_request": HTTPStatus.BAD_REQUEST,
    "forbidden": HTTPStatus.FORBIDDEN,
    "game_not_found": HTTPStatus.NOT_FOUND,
    "not_found": HTTPStatus.NOT_FOUND,
    "method_not_allowed": HTTPStatus.METHOD_NOT_ALLOWED,
    "illegal_action": HTTPStatus.CONFLICT,
    "payload_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
    "internal_error": HTTPStatus.INTERNAL_SERVER_ERROR,
}


def _error(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def route(method: str, path: str, body: Any) -> tuple[str, dict] | dict:
    """Map an HTTP request to a protocol request, or return an error envelope."""
    body = body if isinstance(body, dict) else {}
    if path == "/v1/games":
        return ("new_game", {"players": body.get("players"), "script": body.get("script")}) if method == "POST" \
            else _error("method_not_allowed", "use POST")
    if path == "/v1/scripts/random":
        return ("random_script", {"seed": body.get("seed")}) if method == "POST" else _error("method_not_allowed", "use POST")
    if path == "/v1/reveal":
        return ("reveal", {}) if method == "GET" else _error("method_not_allowed", "use GET")
    match = _GAME_ROUTE.match(path)
    if not match:
        return _error("not_found", "no such route")
    game_id, what = match["game_id"], match["what"]
    if what == "actions":
        return ("act", {"game_id": game_id, "action": body}) if method == "POST" else _error("method_not_allowed", "use POST")
    if method != "GET":
        return _error("method_not_allowed", "use GET")
    return ("legal_actions" if what == "legal-actions" else "view", {"game_id": game_id})


class PressureService(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], box: BlackBox, allow_reveal: bool = False) -> None:
        super().__init__(address, _Handler)
        self.box = box
        self.allow_reveal = allow_reveal
        self.lock = threading.Lock()  # the game store is not thread-safe

    def handle(self, method: str, path: str, body: Any) -> dict:
        if path == "/health":
            return {"ok": True, "result": {"service": SERVICE, "version": VERSION}} if method == "GET" \
                else _error("method_not_allowed", "use GET")
        routed = route(method, path, body)
        if isinstance(routed, dict):
            return routed
        name, params = routed
        with self.lock:
            response = handle_request(self.box, {"method": name, "params": params}, self.allow_reveal)
        response.pop("id", None)
        return response


class _Handler(BaseHTTPRequestHandler):
    server: PressureService
    server_version = f"{SERVICE}/{VERSION}"
    sys_version = ""

    def do_GET(self) -> None:
        self._serve("GET", None)

    def do_POST(self) -> None:
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit():
            return self._send(_error("invalid_request", "bad Content-Length"))
        if int(length) > MAX_BODY:
            self.close_connection = True  # the unread body must not be parsed as the next request
            return self._send(_error("payload_too_large", f"body over {MAX_BODY} bytes"))
        raw = self.rfile.read(int(length)) if int(length) else b"{}"
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return self._send(_error("invalid_request", "body is not valid JSON"))
        self._serve("POST", body)

    def do_PUT(self) -> None:
        self._send(_error("method_not_allowed", "use GET or POST"))

    do_DELETE = do_PATCH = do_PUT

    def _serve(self, method: str, body: Any) -> None:
        try:
            response = self.server.handle(method, self.path.split("?", 1)[0], body)
        except Exception:  # one bad request must never take the service down
            response = _error("internal_error", "internal error")
        self._send(response)

    def _send(self, response: dict) -> None:
        status = HTTPStatus.OK if response.get("ok") else STATUS_FOR_ERROR.get(response["error"]["kind"], 400)
        data = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # keep request logs off stdout
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strata black box (HTTP microservice)")
    add_box_arguments(parser)
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default 127.0.0.1, local only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    service = PressureService((args.host, args.port), build_box(args), args.allow_reveal)
    print(f"pressure service on http://{args.host}:{service.server_address[1]}", flush=True)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
