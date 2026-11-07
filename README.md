# Strata

> Dig through a black box, layer by layer, until you can rebuild it and prove it behaves the same.

Strata is a software archaeology agent. It treats a card/dice game (Exploding Kittens-style first, Bang!-style as a stretch goal) as an unfamiliar, authorized black box. It probes the game, records runtime traces, tracks hypotheses in an evidence ledger, rebuilds the game in a new language, and proves behavioral equivalence.

The hackathon theme this answers:

> Recover, rebuild, port or extend authorized software from incomplete evidence such as binaries, runtime traces, partial code or documentation. Ship behavioral evidence that separates what was observed from what was inferred. How can a system map an unfamiliar program, track its uncertain hypotheses, and prove behavioral equivalence across platforms, languages or runtimes?

---

## 1. Goal

Build a system that can:

1. **Map** an unfamiliar program by probing it through a simulation harness.
2. **Track** uncertain hypotheses in a ledger where every claim cites evidence.
3. **Rebuild** the program in a different language/runtime from confirmed rules only.
4. **Prove** equivalence with replayed traces, differential fuzzing, and an honest report of gaps.
5. **Show** that it learned from observation, not from the model's memory of the game.

### How the problem statement maps to Strata

| Problem statement phrase | Strata component |
|---|---|
| "authorized software" | A black-box game we wrote ourselves (`blackbox/`) |
| "incomplete evidence" (binaries, runtime traces, partial docs) | JSONL traces, opaque interface, optional partial notes |
| "map an unfamiliar program" | Probe loop + rule-interaction matrix |
| "track its uncertain hypotheses" | Evidence ledger (Observed / Inferred / Confirmed / Refuted / Open) |
| "separates what was observed from what was inferred" | Ledger rules: every claim cites trace IDs; inference is labeled as such |
| "recover, rebuild, port, extend" | Generated rebuild in JavaScript (Rust optional) + one safe extension |
| "prove behavioral equivalence" | Trace replay, differential fuzzing, counterexample shrinking |
| "across platforms, languages or runtimes" | Python original vs. JavaScript rebuild (Node and browser) |

---

## 2. Core idea

```
   ┌─────────┐   ┌────────────┐   ┌────────┐   ┌─────────┐   ┌────────┐
   │  PROBE  │ → │ HYPOTHESIZE│ → │  TEST  │ → │ REBUILD │ → │ VERIFY │
   └─────────┘   └────────────┘   └────────┘   └─────────┘   └────────┘
     traces        ledger +         separating     generated      replay +
     + surprises   LLM proposals    experiments    new-language   fuzz + report
```

Principle: **the LLM proposes, the deterministic engine decides.** The model may suggest any rule, including ones it remembers from the real game. A rule only becomes Confirmed if it predicts held-out traces it never saw.

---

## 3. Ledger statuses

| Status | Meaning |
|---|---|
| **Observed** | Directly seen in a trace. No interpretation. Cites trace IDs. |
| **Inferred** | A proposed rule that fits observations but has not been separated from alternatives. |
| **Confirmed** | Predicts held-out traces and beat competing hypotheses in a separating experiment. |
| **Refuted** | A counterexample trace exists. Keep it, with the trace ID. |
| **Open** | Never tested. Shown as a blank square in the interaction matrix. |

Extra rules:
- A claim with no supporting trace ID is rejected.
- Confirmed rules that are not retested in new contexts decay back to Inferred.
- The agent logs a prediction before each probe. Wrong predictions go in the **surprise log** and drive the next probes.

---

## 4. Handling randomness and hidden information

These games have shuffled decks, dice, and hidden hands, so equivalence needs care:

- **Test mode:** the black box accepts a stacked deck / scripted dice. Traces become deterministic and replayable, so equivalence is exact.
- **Rules vs. chance:** the agent recovers transition rules (exact) separately from distributions (statistical test with a stated confidence level).
- **Hidden state:** inferred through repeated probes with the same seed and different actions.

---

## 5. Proving it learned (not recalled)

Because the model may already know these games, Strata includes controls:

| Control | What it shows |
|---|---|
| Mutated rules | 2-3 rule changes per seed (e.g. defuse only works on even turns). Reciting the real rules fails visibly. |
| Opaque tokens mode | Cards/faces/actions renamed `c1..cN`, `f1..f6`, `a1..aK`. |
| Predict-then-check | Predictions logged before each probe; surprises prove evidence is driving. |
| Held-out traces | Rules must predict traces not used to fit them. |
| Zero-probe ablation | With no probes the rebuild scores low; score rises with probes (learning curve). |

Honest claim: the accepted rules are determined by the traces, not by recall. We cannot prove the model had zero influence on which hypotheses it thought of first.

---

## 6. Planned structure

```
strata/
├── README.md
├── blackbox/              # the "unknown" original (Python)
│   ├── game.py            # card game engine, hidden from the agent
│   ├── mutations.py       # rule mutators
│   └── cli.py             # stdin/stdout interface the agent drives
├── engine/                # game-agnostic archaeology core
│   ├── recorder.py        # JSONL trace recorder
│   ├── ledger.py          # hypothesis ledger + status rules
│   ├── probe.py           # probe loop, prediction + surprise log
│   ├── experiments.py     # separating-experiment designer
│   ├── scorer.py          # executable hypothesis scoring on held-out traces
│   └── matrix.py          # rule-interaction matrix
├── agent/
│   └── propose.py         # LLM hypothesis proposals (executable predicates)
├── rebuild/
│   ├── generate.py        # ledger -> new-language source
│   └── js/                # generated JavaScript rebuild (pure step function)
├── verify/
│   ├── replay.py          # replay all traces on both versions
│   ├── fuzz.py            # differential fuzzing
│   ├── shrink.py          # counterexample shrinking
│   └── mutation_test.py   # do tests catch broken rebuilds?
├── report/
│   └── build_report.py    # one-page evidence report
├── demo/
│   └── dashboard.py       # live terminal ledger view
└── traces/                # recorded evidence (JSONL)
```

---

## 7. To-do list

### Must have
- [ ] Black-box card game: 8-10 card types, 2-3 players, stacked-deck test mode
- [ ] Rule mutators (2-3 per seed) and opaque-token output mode
- [ ] Narrow interface: legal actions, one seat's visible state, outcome
- [ ] JSONL trace recorder with trace IDs
- [ ] Evidence ledger enforcing the five statuses and citation rule
- [ ] Probe loop with predict-then-check and surprise log
- [ ] Executable hypothesis format and held-out scorer
- [ ] At least 3 separating experiments (e.g. defuse behavior, attack stacking, turn order)
- [ ] Rebuild generator emitting a pure `step(state, action)` in JavaScript
- [ ] Trace replay on original vs. rebuild, with diff output
- [ ] Differential fuzzing over thousands of scripted games
- [ ] One-page evidence report

### Strong extras (pick two)
- [ ] Counterexample shrinking
- [ ] Mutation testing of the rebuild
- [ ] Faithful vs. fixed build (preserve quirk or flag as bug)
- [ ] Rule-interaction matrix visual
- [ ] Zero-probe ablation / learning curve
- [ ] One safe extension (e.g. undo), proven not to change behavior when off

### Stretch
- [ ] Bang!-style dice game adapter (rerolls, seat distance, hidden roles)
- [ ] Second rebuild target (Rust or Go)
- [ ] Statistical fairness audit of the shuffle

---

## 8. Six-hour plan

**Hours 0-3 (observe):** write the black box with test mode and mutations; build the recorder and ledger; add the probe loop with predictions and surprises; wire the LLM to propose executable hypotheses and the scorer to accept or reject them.

**Hours 3-6 (rebuild and prove):** generate the JavaScript rebuild from confirmed rules; replay all traces and fuzz both versions; shrink any divergence; produce the report. Reserve the last 30 minutes for demo rehearsal.

| Time | Milestone |
|---|---|
| 0:00-1:00 | Black box, test mode, mutations, trace format |
| 1:00-2:00 | Recorder, ledger, probe runner |
| 2:00-3:00 | Hypothesis scorer, separating experiments |
| 3:00-4:00 | Rebuild generator (JavaScript) |
| 4:00-5:00 | Replay, fuzz, shrink |
| 5:00-5:30 | Report |
| 5:30-6:00 | Demo polish and rehearsal |

---

## 9. Demo script

1. Show the black box and an empty ledger.
2. Agent probes; Observed facts fill in live.
3. The agent confidently predicts standard behavior, the prediction fails, and the surprise log lights up (a mutated rule).
4. Competing hypotheses are tested; losers are eliminated on screen.
5. The JavaScript rebuild appears.
6. Side-by-side replay of original vs. rebuild.
7. Final screen: "N games, 0 divergences", plus the report with Proven / Inferred / Untested / Known differences.

---

## 10. Evidence report format

1. **Observed:** raw facts with trace counts.
2. **Inferred and confirmed:** rules, with the experiment that confirmed each.
3. **Open / untested:** blank cells in the interaction matrix, timers, rendering, anything not probed.
4. **Equivalence:** replay and fuzz pass/fail numbers, known differences, and the shortest counterexample if any.

---

## 11. Honest limits

- Equivalence is only as strong as the inputs tested; untested rules stay unknown.
- Exact equivalence holds for scripted randomness. Unscripted randomness gets statistical agreement only.
- Prior model knowledge cannot be fully removed; the controls show it is insufficient, not absent.
- The black box is our own implementation, so this is an authorized exercise. Using the real game names is fine for a private demo, but it makes recall easier, so keep the mutation and opaque-token controls on.

---

## 12. Setup (to be filled in as we build)

```bash
# Python 3.11+ and Node 20+ expected
cd strata
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# run the probe loop against the black box
python -m engine.probe --seed 42 --mutations 2

# generate and verify the rebuild
python -m rebuild.generate
python -m verify.replay && python -m verify.fuzz --games 5000

# build the report
python -m report.build_report
```

---

## 13. Success criteria

- Rebuild in a second language passes 100% of recorded traces.
- Zero divergences across the fuzz run, or each divergence is shrunk and documented.
- Ledger shows at least one surprise that led to a discovered mutated rule.
- Learning curve shows score rising with probe count.
- Report clearly separates Observed, Inferred, Confirmed, and Open.
