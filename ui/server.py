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
  GET /api/ledger           the Hypothesizer's ledger, from ``--ledger`` (a JSON file) or, better,
                            straight from the agent's run folder with ``--run-dir``:
                            {"claims": [{"id", "claim", "status", "evidence": [trace ids],
                                         "experiment"}]}
                            status is one of observed | inferred | confirmed | refuted | open.
  GET /api/feed?since=0&follow=1
                            Server-Sent Events: the agent's live action feed from
                            ``--run-dir``/events.jsonl. One ``feed`` event per agent loop:
                            {"loop", "type", "line", "sentence", "next", "at", "trace_ids"}, with the
                            loop number as the SSE id so a reconnect resumes where it left off.
                            ``follow=0`` sends what exists and closes. ``replay=20`` instead re-sends
                            the saved run in order at 20x speed (``0`` = instantly) and closes, for
                            rehearsing the demo or recovering when live calls are slow.

Ask box and live build view (T16; these need ``--run-dir``)
  POST /api/ask             {"text": "rebuild it in Go"} -> {"ok", "reply", "running"}. One line in, one
                            plain answer out. A rebuild starts in the background and the reply returns at once.
  POST /api/cancel          stop the running stage; progress is saved and "start" resumes it
  GET /api/agent            {"enabled", "running", "status", "models", "budget", "messages"}
  GET /api/agent-traces?since=0&delay=0.4&follow=1
                            Server-Sent Events in the same shape as /api/stream, built from the
                            agent's own probe games (``game``, ``trace``, ``end``). The ``end`` event
                            carries the SSE id (games delivered) so a reconnect resumes.
  GET /api/files            the files in the run's rebuild folder (read-only, jailed to that folder)
  GET /api/files/read?path= one file's text (size-capped, firewall-checked)
  GET /api/files/stream     Server-Sent Events: ``files`` (the list plus what changed) as the agent writes.
Everything is local only: the server binds to 127.0.0.1 and refuses other Host or Origin headers.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import signal
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agent_bridge import AgentBridge
from .agent_traces import GameTail, game_events
from .project_files import FileJail, PathRefused, diff_fingerprints

_ROOT = Path(__file__).resolve().parents[1]
for _extra in (_ROOT / "agent", _ROOT / "services" / "pressure"):
    # agent packages (ledger, livefeed...) and the black box the live view plays; appended, never shadowing
    if str(_extra) not in sys.path:
        sys.path.append(str(_extra))

from blackbox.client import BlackBoxClient
from simulate.recorder import TraceRecorder
from simulate.run import RunConfig, play_game, spec_for

from livefeed.main.export import ui_event, ui_ledger  # noqa: E402
from livefeed.main.events import read_events  # noqa: E402
from livefeed.main.stream import follow, replay_events  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
PAGE_FILES = {
    "index.html", "style.css", "app.js", "arena.js", "ask.js", "backstage.js", "cards.js", "observed.js", "results.js",
    "words.js",
}
# bundled art so the demo works offline (OpenMoji CC BY-SA 4.0, DiceBear Notionists CC0)
ASSET_FILES = {
    f"assets/{path.parent.name}/{path.name}"
    for path in (STATIC_DIR / "assets").glob("*/*.svg")
    if path.parent.name in ("cards", "faces")
}
STATIC_FILES = frozenset(PAGE_FILES | ASSET_FILES)
HEARTBEAT_SECONDS = 10.0
MAX_BODY_BYTES = 4096
FILE_POLL_SECONDS = 0.5
TRACE_POLL_SECONDS = 0.5
LEDGER_STATUSES = ("observed", "inferred", "confirmed", "refuted", "open")


class StreamParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    seed: int = Field(default=42, ge=0, le=1_000_000_000)
    mutations: int = Field(default=2, ge=0, le=6)
    delay: float = Field(default=1.0, ge=0.0, le=3.0)
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
    run_dir: Path | None = None
    bridge: AgentBridge | None = None

    def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
        return

    # ---------- local-only guards (T16.8) ----------
    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self._allowed_hosts()

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin.removeprefix("http://") in self._allowed_hosts()

    def _read_json(self) -> dict | None:
        """The request body as a dict, or None after sending the right error."""
        if self.headers.get_content_type() != "application/json":
            self._json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY_BYTES:
            self._json({"error": "body must be between 1 and %d bytes" % MAX_BODY_BYTES},
                       HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > MAX_BODY_BYTES else HTTPStatus.BAD_REQUEST)
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "body is not valid JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(body, dict):
            self._json({"error": "body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            return None
        return body

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if not self._host_ok() or not self._origin_ok():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        if url.path not in ("/api/ask", "/api/cancel"):
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if self.bridge is None:
            return self._json({"ok": False, "reply": "The agent is not attached to this server. "
                                                     "Start it with --run-dir agent/runs/<run_id>."})
        body = self._read_json()
        if body is None:
            return
        if url.path == "/api/cancel":
            return self._json(self.bridge.cancel())
        reply = self.bridge.ask(body.get("text"))
        return self._json(reply, HTTPStatus.OK if reply.get("ok") else HTTPStatus.BAD_REQUEST)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urlparse(self.path)
        if not self._host_ok():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        if url.path in ("/", "/index.html"):
            return self._static("index.html")
        if url.path.startswith("/static/"):
            return self._static(url.path.removeprefix("/static/"))
        if url.path == "/api/ledger":
            if self.ledger_path is None and self.run_dir is not None:
                return self._json(ui_ledger(self.run_dir))
            return self._json(read_ledger(self.ledger_path))
        if url.path == "/api/feed":
            return self._feed(parse_qs(url.query))
        if url.path == "/api/agent":
            return self._json(self.bridge.status() if self.bridge else {"enabled": False})
        if url.path == "/api/agent-traces":
            return self._agent_traces(parse_qs(url.query))
        if url.path == "/api/files":
            jail = self._jail()
            return self._json({"connected": jail is not None, "files": jail.list() if jail else []})
        if url.path == "/api/files/read":
            return self._file_read(parse_qs(url.query))
        if url.path == "/api/files/stream":
            return self._files_stream(parse_qs(url.query))
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

    def _send_event(self, name: str, payload: dict, event_id: int | None = None) -> None:
        ident = f"id: {event_id}\n" if event_id is not None else ""
        try:
            self.wfile.write(f"{ident}event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ClientGone from error

    def _feed(self, query: dict) -> None:
        try:  # a reconnecting browser sends Last-Event-ID; it wins over ?since=
            since = int(self.headers.get("Last-Event-ID") or query.get("since", ["0"])[0])
        except ValueError:
            return self._json({"error": "since must be a whole number"}, HTTPStatus.BAD_REQUEST)
        live = query.get("follow", ["1"])[0] != "0"
        try:
            replay = float(query["replay"][0]) if "replay" in query else None
        except ValueError:
            return self._json({"error": "replay must be a number"}, HTTPStatus.BAD_REQUEST)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self._send_event("status", {"connected": self.run_dir is not None,
                                        "run": self.run_dir.name if self.run_dir else None})
            if self.run_dir is None:
                return
            quiet_since = time.monotonic()

            def beat() -> None:
                nonlocal quiet_since
                if time.monotonic() - quiet_since >= HEARTBEAT_SECONDS:
                    quiet_since = time.monotonic()
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError) as error:
                        raise ClientGone from error

            if replay is not None:  # a saved run, re-sent in order at `replay` times real speed
                for event, history in replay_events(self.run_dir, replay):
                    self._send_event("feed", ui_event(event, history), event_id=event.loop)
                return
            for event in follow(self.run_dir, max(0, since), stop=lambda: not live, idle=beat):
                self._send_event("feed", ui_event(event, read_events(self.run_dir)[: event.loop]),
                                 event_id=event.loop)
                quiet_since = time.monotonic()
        except ClientGone:
            return

    # ---------- T16.3: the agent's own probe games ----------
    def _agent_traces(self, query: dict) -> None:
        try:
            since = int(self.headers.get("Last-Event-ID") or query.get("since", ["0"])[0])
            delay = min(3.0, max(0.0, float(query.get("delay", ["0.4"])[0])))
        except ValueError:
            return self._json({"error": "since must be a whole number and delay a number"}, HTTPStatus.BAD_REQUEST)
        live = query.get("follow", ["1"])[0] != "0"
        from llm.main import firewall

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self._send_event("status", {"connected": self.run_dir is not None, "source": "agent",
                                        "run": self.run_dir.name if self.run_dir else None})
            if self.run_dir is None:
                return
            tail = GameTail(self.run_dir / "probe_games.jsonl", skip=max(0, since))
            self._send_event("run", {"run_id": self.run_dir.name, "source": "agent", "start": max(0, since)})
            quiet_since = time.monotonic()
            while True:
                for index, game in tail.poll():
                    try:
                        firewall.check(json.dumps(game))
                    except firewall.PromptFirewallError:
                        continue                      # restricted text never reaches the page
                    for name, payload in game_events(game, index):
                        self._send_event(name, payload, event_id=index + 1 if name == "end" else None)
                        if name == "trace" and delay:
                            time.sleep(delay)
                    quiet_since = time.monotonic()
                if not live:
                    return
                if time.monotonic() - quiet_since >= HEARTBEAT_SECONDS:
                    quiet_since = time.monotonic()
                    self._ping()
                time.sleep(TRACE_POLL_SECONDS)
        except ClientGone:
            return

    def _ping(self) -> None:
        try:
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise ClientGone from error

    # ---------- T16.5: the folder the agent is writing code into ----------
    def _jail(self) -> FileJail | None:
        if self.run_dir is None:
            return None
        from rebuilder.main.location import rebuild_dir
        return FileJail(rebuild_dir(self.run_dir))

    def _file_read(self, query: dict) -> None:
        jail = self._jail()
        if jail is None:
            return self._json({"error": "no agent run attached"}, HTTPStatus.NOT_FOUND)
        try:
            return self._json(jail.read(query.get("path", [""])[0]))
        except PathRefused as error:
            return self._json({"error": str(error)}, HTTPStatus.NOT_FOUND)

    def _files_stream(self, query: dict) -> None:
        live = query.get("follow", ["1"])[0] != "0"
        jail = self._jail()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self._send_event("status", {"connected": jail is not None})
            if jail is None:
                return
            before: dict[str, tuple[int, int]] = {}
            first, quiet_since = True, time.monotonic()
            while True:
                files = jail.list()
                now = {item["path"]: (item["size"], item["mtime"]) for item in files}
                changes = diff_fingerprints(before, now)
                if first or changes:
                    self._send_event("files", {"files": files, "changes": [] if first else changes})
                    quiet_since = time.monotonic()
                first, before = False, now
                if not live:
                    return
                if time.monotonic() - quiet_since >= HEARTBEAT_SECONDS:
                    quiet_since = time.monotonic()
                    self._ping()
                time.sleep(FILE_POLL_SECONDS)
        except ClientGone:
            return

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


def make_server(port: int, ledger: Path | None = None, traces_dir: Path = Path("traces"),
                run_dir: Path | None = None, bridge: AgentBridge | None = None) -> ThreadingHTTPServer:
    handler = type("StrataHandler", (Handler,),
                   {"ledger_path": ledger, "traces_dir": traces_dir, "run_dir": run_dir, "bridge": bridge})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strata live view")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ledger", type=Path, default=None, help="ledger JSON written by the Hypothesizer")
    parser.add_argument("--traces", type=Path, default=Path("traces"), help="where live runs are recorded")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="the agent's run folder (agent/runs/<run_id>): feeds the ledger and the live action feed")
    parser.add_argument("--no-ask", action="store_true",
                        help="do not attach the agent: no ask box, only the read-only views")
    args = parser.parse_args(argv)
    bridge = None
    if args.run_dir is not None and not args.no_ask:
        run_dir = args.run_dir.resolve()
        bridge = AgentBridge(run_dir.parent, run_dir.name)
    server = make_server(args.port, args.ledger, args.traces, args.run_dir, bridge)
    print(f"Strata live view on http://localhost:{args.port}")  # CLI startup line

    def stop(*_args) -> None:          # Ctrl-C or kill: take the running stage down with the server
        if bridge is not None:
            bridge.shutdown()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if bridge is not None:
            bridge.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
