"""A tiny JSON-lines game used to test the Verifier. `--bug` plants a rare divergence.

State is a counter. draw: +1. play vent: -1 (but with --bug, -2 when the counter is 3).
The game is over at +5. Illegal actions get an `illegal_action` error.
"""
import json
import random
import sys

BUG = "--bug" in sys.argv
CRASH = "--crash" in sys.argv
LENIENT = "--lenient" in sys.argv
games = {}


def view(g):
    return {"players": g["players"], "hands": [[], []], "alive": [True] * g["players"],
            "discard": [], "draw_pile_size": 10, "current_seat": 0, "actions_left": 1,
            "pressure": g["n"], "turn": g["turn"], "over": g["n"] >= 5,
            "winner": 0 if g["n"] >= 5 else None}


def handle(method, params):
    if method == "random_script":
        rng = random.Random(params["seed"])
        return {"deck": [rng.choice(["vent", "gear1"]) for _ in range(42)]}
    if method == "new_game":
        gid = f"g-{len(games) + 1:04d}"
        games[gid] = {"players": params["players"], "n": 0, "turn": 1}
        return {"game_id": gid, "view": view(games[gid])}
    g = games.get(params.get("game_id"))
    if g is None:
        raise KeyError("game_not_found")
    if method == "view":
        return view(g)
    legal = [] if g["n"] >= 5 else [{"type": "draw"}, {"type": "play", "card": "vent"}]
    if method == "legal_actions":
        return legal
    if method == "act":
        if params["action"] not in legal:
            if not LENIENT or not legal:
                raise ValueError("illegal_action")
            params["action"] = {"type": "draw"}
        if CRASH and g["n"] == 4:
            sys.exit(3)
        before = g["n"]
        g["n"] += 1 if params["action"]["type"] == "draw" else (-2 if BUG and before == 3 else -1)
        g["turn"] += 1
        return {"events": [{"type": "pressure", "value": g["n"]}], "view": view(g)}
    raise NotImplementedError(method)


for line in sys.stdin:
    request = json.loads(line)
    try:
        result = handle(request["method"], request.get("params", {}))
        print(json.dumps({"id": request["id"], "ok": True, "result": result}), flush=True)
    except KeyError:
        print(json.dumps({"id": request["id"], "ok": False,
                          "error": {"kind": "game_not_found", "message": "x"}}), flush=True)
    except ValueError:
        print(json.dumps({"id": request["id"], "ok": False,
                          "error": {"kind": "illegal_action", "message": "x"}}), flush=True)
