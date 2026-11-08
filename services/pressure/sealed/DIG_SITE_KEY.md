# Dig-site answer key (sealed)

**Sealed. Never give this file to an agent.** It records exactly how each dig-site artifact differs from the truth, so the final report can score what Strata caught. The true rules are in `RULES.md`. The mutations planted in a given build are in `sealed/runs/<run_id>.json`, which is written by the builder and never committed.

## Documentation: `docs/pressure-manual-v1.md`

The manual describes the base rules only, so it is also wrong about every mutation active in the build. On top of that, it has these fixed errors, which are wrong in every build:

| Topic | Manual says | Truth | How traces show it |
|---|---|---|---|
| Hand limit | You may hold up to 7 cards | Drawing is legal only below 6 cards, so the most a hand holds is 6 | `draw` is never in the legal actions when the hand has 6 cards |
| Scan | Shows the top 3 cards | Shows the top 2, or 1 under `short_scan` | `revealed` events never list more than 2 cards |
| Game end | Ends when one player remains | Also ends with no winner when every living hand and the draw pile are empty | A `game_over` event with `winner: null` |

The manual is right about setup, the gear values, the pressure limit of 12, and the overflow reset to 0, unless a mutation such as `hot_restart` changes it.

## Partial code: `source/`

- `engine.py` is the real engine with 6 function bodies removed: `_gear`, `_vent`, `_reverse`, `_double`, `_jam` and `_draw`. Every mutation's behavior lives in one of these.
- `state.py` is complete.
- `rules.py`, `cards.py`, `api.py` and `view.py` are missing entirely, so rule values such as `vent_amount` or `hand_limit` cannot be read from the code.
- Two rule field names remain visible where kept functions use them: `rules.hand_limit` in `legal_actions` and `rules.scan_depth` in `_scan`. They show that these values are configurable, but not what they are.
- Comments are stripped. Docstrings, names and type hints are kept.

## Binary: `bin/pressure.pyz`

- The runnable black box: the real game with the build's mutations baked in.
- Compiled Python bytecode with docstrings and asserts stripped (`-OO`), no source files, in a compressed zip. It ignores every command-line flag, so its mutations cannot be changed or revealed.
- Limit: a bytecode decompiler could recover the logic. The dig-site rules allow agents to run it and inspect it like a stripped binary (size, hash, entry point, `strings`), not to decompile it.

## Runtime traces: `traces/seed-corpus/`

- Recorded by playing the same build: same seed, so the same mutations.
- Correct but incomplete: random and fixed-policy play rarely hits some interactions.
- Scenario names are replaced by neutral IDs (`s01`…`s12`), because names such as `jam_then_draw` point at specific rules.
