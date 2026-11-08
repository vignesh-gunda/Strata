# Pressure service

The original program that Strata reverse-engineers. **Agents never read this folder.** They get only the `dig-site/` folder at the repo root, which this service generates.

Pressure is an original card game for 2 to 4 players with 8 cards (`gear1`, `gear2`, `gear3`, `vent`, `reverse`, `double`, `jam`, `scan`) and up to 6 hidden rule mutations per run. The true rules are in [`sealed/RULES.md`](sealed/RULES.md).

## Layout

| Path | What it is |
|---|---|
| `blackbox/` | The game: pure engine, session API, and two transports |
| `blackbox/server.py` | Transport 1: JSON lines over stdin/stdout |
| `blackbox/http_server.py` | Transport 2: HTTP microservice |
| `blackbox/client.py` | Python client that starts any JSON-lines program as a separate process |
| `simulate/` | Batch simulator that records traces |
| `tools/build_dig_site.py` | Builds the dig-site evidence from this code |
| `tools/assets/` | Hand-written inputs to the dig-site (the stale manual) |
| `sealed/` | Answer keys: the true rules, how each dig-site artifact differs from the truth, and the mutations planted in each build (`sealed/runs/`, never committed) |
| `tests/` | 74 tests |

## Setup

Tested on Python 3.12, which the dig-site binary requires.

```bash
cd services/pressure
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest
```

`pytest.ini` disables the `pytest-asyncio` plugin, which crashes with an internal error in some environments.

## Run the game

Both transports use the same validation, dispatcher and error kinds.

```bash
# JSON lines over stdin/stdout (what Strata's client and the dig-site binary use)
python3 -m blackbox.server --seed 42 --mutations 2

# HTTP on 127.0.0.1:8710
python3 -m blackbox.http_server --seed 42 --mutations 2
```

Shared flags:

| Flag | Meaning |
|---|---|
| `--seed N` | Run seed. Picks which mutations are planted. |
| `--mutations N` | How many mutations to plant, 0 to 6 |
| `--mutation NAME` | Plant a named mutation instead (repeatable) |
| `--opaque` | Rename cards, actions and event types to `c1..`, `a1..`, `e1..` |
| `--allow-reveal` | Enable `reveal`, which returns the planted mutations. For reports only. |

The HTTP server binds to `127.0.0.1` by default. It has no authentication, so don't bind it to a public interface.

### Container

```bash
docker build -t strata-pressure services/pressure
docker run --rm -p 127.0.0.1:8710:8710 -e SEED=42 -e MUTATIONS=2 strata-pressure
```

The container runs as a non-root user (uid 10001). Publish the port on `127.0.0.1` only, as above, because the service has no authentication.

## Contract

Strata, the Verifier and every rebuild must speak this exactly. Changing it breaks all of them.

### Methods

| Method | Params | Result |
|---|---|---|
| `random_script` | `seed: int` | `{"deck": [card, ...]}`, a shuffled 42-card deck |
| `new_game` | `players: 2-4`, `script: {"deck": [...]}` | `{"game_id": "g-0001", "view": View}` |
| `legal_actions` | `game_id` | `[Action, ...]`, sorted by type then card |
| `act` | `game_id`, `action: Action` | `{"events": [Event, ...], "view": View}` |
| `view` | `game_id` | `View` |
| `reveal` | none | `{"mutations": [...], "cards": {...}, "actions": {...}, "events": {...}}`. Needs `--allow-reveal`. |

### JSON-lines framing

```json
{"id": 1, "method": "act", "params": {"game_id": "g-0001", "action": {"type": "play", "card": "gear2"}}}
{"id": 1, "ok": true, "result": {"events": [...], "view": {...}}}
{"id": 1, "ok": false, "error": {"kind": "illegal_action", "message": "..."}}
```

### HTTP routes

Responses use the same envelope without `id`.

| Route | Method | HTTP status on success |
|---|---|---|
| `GET /health` | — | 200 |
| `POST /v1/scripts/random` | body `{"seed": 7}` | 200 |
| `POST /v1/games` | body `{"players": 3, "script": {...}}` | 200 |
| `GET /v1/games/<game_id>/legal-actions` | — | 200 |
| `POST /v1/games/<game_id>/actions` | body is the Action | 200 |
| `GET /v1/games/<game_id>/view` | — | 200 |
| `GET /v1/reveal` | — | 200, or 403 without `--allow-reveal` |

### Error kinds

| Kind | HTTP status | Meaning |
|---|---|---|
| `invalid_request` | 400 | Malformed request or bad params |
| `forbidden` | 403 | `reveal` without `--allow-reveal` |
| `game_not_found` | 404 | Unknown `game_id` |
| `not_found` | 404 | Unknown HTTP route |
| `method_not_allowed` | 405 | Wrong HTTP method for the route |
| `illegal_action` | 409 | Well formed, but not legal right now |
| `payload_too_large` | 413 | HTTP body over 1 MB |
| `internal_error` | 500 | Unexpected failure; the process keeps running |

### Action

```json
{"type": "play", "card": "gear2"}
{"type": "draw"}
{"type": "pass"}
```

### Events

| Type | Fields |
|---|---|
| `played` | `seat`, `card` |
| `pressure` | `value` |
| `eliminated` | `seat` |
| `revealed` | `seat`, `cards` |
| `drew` | `seat`, `card` |
| `passed` | `seat` |
| `turn_passed` | `to`, `actions`, `turn` |
| `game_over` | `winner` (a seat, or `null`) |

An action can produce no effect events. For example, a jammed card play produces only `played`.

### View

```json
{"players": 3, "hands": [["gear1", "vent"], [], ["jam"]], "alive": [true, false, true],
 "discard": ["gear3"], "draw_pile_size": 17, "current_seat": 2, "actions_left": 1,
 "pressure": 5, "turn": 9, "over": false, "winner": null}
```

The view never shows the draw pile's order, the turn direction, jams, pending extra actions, the pressure limit, or the mutations.

### Script

`{"deck": [card, ...]}`. Seats are dealt 4 cards round-robin from the top, and the rest becomes the draw pile, top first. The deck is the game's only source of randomness, so the same script and actions always give the same results.

## Build the dig-site

```bash
python3 -m tools.build_dig_site --seed 42 --mutations 2 --games 40 --run-id r-01
```

This rewrites the generated parts of `../../dig-site/` and leaves its `README.md` alone. It also:

- checks that the binary gives the same reply as this service on every step of 20 games
- checks that the binary's raw bytes contain no mutation or rule-field names
- seals the planted mutations in `sealed/runs/<run_id>.json`

The binary, the seed corpus and `MANIFEST.json` are not committed. Run the command after cloning.

## Known limits

- **Opaque mode isn't fully opaque.** Card, action and event names are renamed, but view keys such as `pressure` and `hands` are not. The seed corpus is built without opaque mode.
- **The binary is Python bytecode.** A decompiler could recover its logic. The dig-site rules let agents run and inspect it, not decompile it.
- **The binary needs Python 3.12 and pydantic.** Bytecode only runs on the Python version that built it.
