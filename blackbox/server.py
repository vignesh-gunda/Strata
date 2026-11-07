"""Run the black box as its own process speaking JSON lines on stdin/stdout.

Request:  {"id": 1, "method": "act", "params": {"game_id": "g-0001", "action": {...}}}
Response: {"id": 1, "ok": true, "result": {...}}
     or:  {"id": 1, "ok": false, "error": {"kind": "illegal_action", "message": "..."}}

Usage: python -m blackbox.server --seed 42 --mutations 2 [--opaque] [--allow-reveal]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from blackbox.api import BlackBox, GameNotFoundError
from blackbox.engine import IllegalActionError
from blackbox.rules import parse_mutation, pick_mutations

MAX_LINE = 1_000_000


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int | str | None = None
    method: Literal["new_game", "random_script", "legal_actions", "act", "view", "reveal"]
    params: dict[str, Any] = Field(default_factory=dict)


def _error(request_id, kind: str, message: str) -> dict:
    return {"id": request_id, "ok": False, "error": {"kind": kind, "message": message}}


def _dispatch(box: BlackBox, request: RequestModel, allow_reveal: bool) -> Any:
    params = request.params
    if request.method == "new_game":
        return box.new_game(params.get("players"), params.get("script"))
    if request.method == "random_script":
        return box.random_script(params.get("seed"))
    if request.method == "legal_actions":
        return box.legal_actions(params.get("game_id"))
    if request.method == "act":
        return box.act(params.get("game_id"), params.get("action"))
    if request.method == "view":
        return box.view(params.get("game_id"))
    if not allow_reveal:
        raise PermissionError("reveal is disabled; start the server with --allow-reveal")
    return box.reveal()


def handle_line(box: BlackBox, line: str, allow_reveal: bool = False) -> dict:
    if len(line) > MAX_LINE:
        return _error(None, "invalid_request", "request line too long")
    try:
        request = RequestModel.model_validate(json.loads(line))
    except (json.JSONDecodeError, ValidationError, RecursionError) as error:
        return _error(None, "invalid_request", type(error).__name__)
    try:
        return {"id": request.id, "ok": True, "result": _dispatch(box, request, allow_reveal)}
    except IllegalActionError as error:
        return _error(request.id, "illegal_action", str(error))
    except GameNotFoundError as error:
        return _error(request.id, "game_not_found", f"no game {error}")
    except PermissionError as error:
        return _error(request.id, "forbidden", str(error))
    except (ValueError, TypeError) as error:
        return _error(request.id, "invalid_request", str(error))
    except Exception:  # never let one bad request kill every game in the process
        return _error(request.id, "internal_error", "internal error")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strata black box (JSON lines over stdio)")
    parser.add_argument("--seed", type=int, default=0, help="run seed; picks hidden mutations")
    parser.add_argument("--mutations", type=int, default=0, help="how many mutations to plant")
    parser.add_argument("--mutation", action="append", default=[], help="plant a named mutation (repeatable)")
    parser.add_argument("--opaque", action="store_true", help="rename cards, actions and events to c1.. / a1.. / e1..")
    parser.add_argument("--opaque-seed", type=int, default=None, help="token-map seed (default: secret random)")
    parser.add_argument("--allow-reveal", action="store_true", help="allow the reveal method (reports only)")
    return parser.parse_args(argv)


def build_box(args: argparse.Namespace) -> BlackBox:
    named = tuple(parse_mutation(name) for name in args.mutation)
    mutations = named or pick_mutations(args.seed, args.mutations)
    token_seed = args.opaque_seed if args.opaque_seed is not None else secrets.randbelow(2**31)
    return BlackBox(mutations, opaque_seed=token_seed if args.opaque else None)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    box = build_box(args)
    for line in sys.stdin:
        if line.strip():
            response = handle_line(box, line, args.allow_reveal)
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
