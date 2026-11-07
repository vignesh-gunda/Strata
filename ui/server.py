"""Strata live view: streams a running simulation to the browser.

    python -m ui.server --port 8765            # then open http://localhost:8765

Routes
  GET /                     the page
  GET /static/<file>        page assets (whitelisted names only)
  GET /api/stream?seed=42&mutations=2&delay=0.15&start=0
                            Server-Sent Events: ``game`` (a game starts), ``trace``
                            (one step, exactly as written to traces.jsonl) and
                            ``end`` (a game finished). Every step is also recorded
                            to traces/<run_id>/ like ``simulate.run``.
  GET /api/ledger           the Hypothesizer's ledger, read from ``--ledger`` if given:
                            {"claims": [{"id", "claim", "status", "evidence": [trace ids],
                                         "experiment"}]}
                            status is one of observed | inferred | confirmed | refuted | open.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blackbox.client import BlackBoxClient
from simulate.recorder import TraceRecorder
from simulate.run import RunConfig, play_game, spec_for

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = frozenset({"index.html", "style.css", "app.js", "table.js", "strata.js", "observed.js"})
LEDGER_STATUSES = ("observed", "inferred", "confirmed", "refuted", "open")


class StreamParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seed: int = Field(default=42, ge=0, le=1_000_000_000)
    mutations: int = Field(default=2, ge=0, le=6)
    delay: float = Field(default=0.15, ge=0.0, le=2.0)
    start: int = Field(default=0, ge=0, le=1_000_000)
    games: int = Field(default=100_000, ge=1, le=100_000)


class ClientGone(Exception):
    """The browser closed the stream."""


def read_ledger(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"connected": False, "claims": []}
    try:
        claims = json.loads(path.read_text(encoding="utf-8")).get("claims", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return {"connected": False, "claims": [], "error": "ledger file is not valid JSON"}
    valid = [claim for claim in claims if isinstance(claim, dict) and claim.get("status") in LEDGER_STATUSES]
    return {"connected": True, "claims": valid}


class Handler(BaseHTTPRequestHandler):
    server_version = "StrataUI/1.0"
    ledger_path: Path | None = None
    traces_dir: Path = Path("traces")

    def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
        return

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._static("index.html")
        if url.path.startswith("/static/"):
            return self._static(url.path.removeprefix("/static/"))
        if url.path == "/api/ledger":
            return self._json(read_ledger(self.ledger_path))
        if url.path == "/api/stream":
            return self._stream(parse_qs(url.query))
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _static(self, name: str) -> None:
        if name not in STATIC_FILES:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_event(self, name: str, payload: dict) -> None:
        try:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ClientGone from error

    def _stream(self, query: dict) -> None:
        try:
            params = StreamParams.model_validate({key: values[0] for key, values in query.items()})
        except ValidationError as error:
            return self._json({"error": "invalid parameters", "detail": error.errors()}, HTTPStatus.BAD_REQUEST)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        config = RunConfig(games=params.games, seed=params.seed, mutations=params.mutations,
                           run_id=f"ui-{params.seed}-{stamp}", out=self.traces_dir)
        self._send_event("run", {"run_id": config.run_id, "seed": params.seed, "start": params.start})
        try:
            with BlackBoxClient(seed=params.seed, mutations=params.mutations) as box:
                with TraceRecorder(self.traces_dir / config.run_id, config.run_id) as recorder:
                    for index in range(params.start, params.start + params.games):
                        game = play_game(
                            box, recorder, spec_for(config, index),
                            on_trace=lambda trace: self._send_event("trace", trace),
                            delay=params.delay,
                            on_start=lambda start: self._send_event("game", start),
                        )
                        self._send_event("end", {key: game[key] for key in ("game_id", "index", "steps", "winner")})
        except ClientGone:
            return


def make_server(port: int, ledger: Path | None = None, traces_dir: Path = Path("traces")) -> ThreadingHTTPServer:
    handler = type("StrataHandler", (Handler,), {"ledger_path": ledger, "traces_dir": traces_dir})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strata live view")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ledger", type=Path, default=None, help="ledger JSON written by the Hypothesizer")
    parser.add_argument("--traces", type=Path, default=Path("traces"), help="where live runs are recorded")
    args = parser.parse_args(argv)
    server = make_server(args.port, args.ledger, args.traces)
    print(f"Strata live view on http://localhost:{args.port}")  # CLI startup line
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
