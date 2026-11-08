# Dig-site: Pressure

This folder is everything Strata's agents get. It holds four kinds of incomplete evidence about an old program called Pressure. The job is to work out how Pressure behaves, rebuild it in JavaScript, and prove the rebuild behaves the same.

**None of this evidence is complete, and some of it is wrong.** That's deliberate.

## Build it first

The binary, the traces and `MANIFEST.json` are generated, not committed. From the repo root:

```bash
cd services/pressure && python3 -m tools.build_dig_site --seed 42 --mutations 2 --games 40 --run-id r-01
```

## What's here

| Path | Kind | Trust tier | What it is |
|---|---|---|---|
| `bin/pressure.pyz` | Binary | Observed (when run) | The running program. Its behavior is the ground truth. |
| `traces/seed-corpus/` | Runtime traces | Observed | Games recorded from this binary. Correct, but they don't cover everything. |
| `source/` | Partial code | Read from code | A recovered fragment of the engine. Some function bodies are lost, and several modules it imports are missing. |
| `docs/pressure-manual-v1.md` | Documentation | Documented | An old player's manual. A later revision is missing, so parts may be out of date. |
| `MANIFEST.json` | Index | — | Every file with its kind, trust tier, size and SHA-256 hash |

## Trust rules

1. **Behavior wins.** Only running the binary can confirm or refute a claim.
2. A claim taken from the manual or the code keeps its tier (Documented or Read from code) until traces back it up.
3. When an artifact and the traces disagree, the artifact is wrong. Mark the claim Refuted and cite the trace ID.

## What agents may do

| Artifact | Allowed | Not allowed |
|---|---|---|
| Binary | Run it. Inspect it as you would a stripped binary: size, hash, entry point, `strings`. | Decompile its bytecode, or unpack it to read code |
| Traces | Read them all | — |
| Partial code | Read it, and run the parts that work | — |
| Manual | Read it | — |
| Anything outside `dig-site/` | — | Read it. In particular, never read `services/`, which holds the original source and the answer keys. |

## Running the binary

It speaks JSON lines on stdin and stdout, and ignores command-line flags. It needs Python 3.12 and `pydantic>=2.7`.

```bash
python3.12 bin/pressure.pyz
```

Requests and replies:

```json
{"id": 1, "method": "random_script", "params": {"seed": 7}}
{"id": 1, "ok": true, "result": {"deck": ["gear3", "gear2", "..."]}}
```

| Method | Params |
|---|---|
| `random_script` | `seed` |
| `new_game` | `players` (2 to 4), `script` (`{"deck": [...]}`) |
| `legal_actions` | `game_id` |
| `act` | `game_id`, `action` (one of the legal actions) |
| `view` | `game_id` |

A failed call returns `{"ok": false, "error": {"kind": ..., "message": ...}}`. The deck in the script is the game's only randomness, so the same script and actions always give the same results.

From Python, the service's client can start the binary as a separate process. Run this from the repo root with `PYTHONPATH=services/pressure`. Pass an absolute path, because the client starts the program from `services/pressure`.

```python
import sys
from pathlib import Path
from blackbox.client import BlackBoxClient

binary = str(Path("dig-site/bin/pressure.pyz").resolve())
with BlackBoxClient(command=[sys.executable, binary]) as box:
    game = box.new_game(3, box.random_script(7))
```

## Trace format

Each line of `traces/seed-corpus/traces.jsonl` is one step:

```json
{"trace_id": "t-000001", "run_id": "r-01", "game_id": "g-0001", "step": 1, "seat": 0, "turn": 1,
 "action": {"type": "play", "card": "gear2"}, "legal": [...], "events": [...], "view": {...},
 "split": "train", "label": "scenario:s01", "prediction": null, "surprise": null}
```

- `split` is `train` or `heldout`. Use held-out games only to score rules, never to fit them.
- `label` says how the game was played: a fixed policy (`policy:<name>`) or a stacked-deck scenario (`scenario:sNN`).
- `prediction` and `surprise` are empty. Strata's Prober fills them in on the games it plays.

`games.jsonl` has one line per game, including the full `script`, so any game can be replayed exactly.
