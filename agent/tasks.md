# Strata agent: task plan

The agent reverse-engineers **Pressure** from `dig-site/` only, rebuilds it in JavaScript, and proves the rebuild matches. Models propose and choose. Code decides what is true.

Status legend: `[ ]` todo, `[x]` done.

## Ground rules (code these in, do not just document them)

1. Artifacts load as **claims with a source and a tier**, never as facts.
2. Only **traces** (corpus traces or traces from new binary runs) can move a claim to Confirmed or Refuted. Manual, code and model output cannot.
3. Missing functions and unknown parameters in the partial code go to the **top of the probe list**.
4. Every disagreement between an artifact and the traces is stored as a **finding**, never discarded.
5. The binary is a **process to run**, never a file to read. Agent tools are `start_binary` and `send_request`. There is no tool that opens `bin/`.
6. **Nothing from `services/` ever enters a prompt**, a tool result or the agent's file access. The agent's file tool is jailed to `dig-site/`.
7. A claim with no trace ID cannot be Confirmed or Refuted.
8. Model-generated code runs in a separate process with a time limit and no API keys in its environment.
9. Keys come from `.env`. They are never logged, printed or sent to a prompt.

## Artifact roles

| Artifact | Role | Tier | Must never |
|---|---|---|---|
| `bin/pressure.pyz` | Judge | Observed (when run) | Be decompiled or unpacked |
| `traces/seed-corpus/` (2,243 steps, 40 games, 8 held-out) | Free evidence | Observed | Be treated as complete. Held-out games only score rules |
| `source/` (partial code) | Map | Read from code | Be trusted for rule values (`rules.py` is absent) |
| `docs/pressure-manual-v1.md` | Leads | Documented | Be trusted at all (it is old and partly wrong) |

## Folder layout

```
agent/
  tasks.md                this file
  <component>/main/       code (import as `from <component>.main.x import y`)
  <component>/tests/      that component's tests only
  Components: llm (T0, done), sandbox (T1, T9), ledger (T2), surveyor (T3),
              auditor (T4), prober (T5), hypothesizer (T6), rebuilder (T7),
              verifier (T8), coordinator (T10, local), report (T11),
              concierge (T14), livefeed (T15)
  rebuild/                JavaScript output (generated)
  runs/                   per-run ledger and traces (gitignored)
  Run all tests: cd agent && .venv/bin/python -m pytest
```

## Sub-agent map

| Sub-agent | Tasks | Model use | Reads | Writes |
|---|---|---|---|---|
| Surveyor | T3 | Muse (claim extraction) | manual, partial code, binary metadata | ledger |
| Auditor | T4 | none (code) and SubQ (triage) | ledger, corpus | ledger, findings |
| Prober | T5 | SubQ (experiment pick) | ledger, binary | traces |
| Hypothesizer | T6 | Muse and SubQ | ledger, traces | ledger |
| Rebuilder | T7 | Muse | Confirmed claims, partial code skeleton | `rebuild/` |
| Verifier | T8 | none | binary, rebuild | verdict |
| Coordinator | T10 | SubQ (routing) | everything it is allowed to show | chat |
| Concierge | T14 | Muse (understand and answer), SubQ (quick routing) | ledger, traces, rebuild, run state | chat, run requests |
| Live feed | T15 | none (code templates) | event log | one-line action stream |

---

## T0. Foundations and model clients

- [x] T0.1 Key smoke test: `scripts/check_keys.py` passes for Muse Spark (`muse-spark-1.3`) and SubQ Snap (`subq-snap-1.1`)
- [x] T0.2 Project setup: `agent/` Python package, `requirements.txt` (`pydantic`, `pytest`), `.venv`, pytest config that disables `pytest-asyncio` like the service does
- [x] T0.3 `llm/muse.py`: wrapper over `POST https://api.meta.ai/v1/responses` with retry and backoff on 429 and 5xx, token accounting, model fallback to `muse-spark-1.1`, JSON-output helper that validates against a pydantic schema and retries once on invalid output
- [x] T0.4 `llm/subq.py`: wrapper over `POST https://api.subq.ai/v1/systemone` with typed helpers `noul()`, `choice()`, `score()`. Returns probabilities and confidence. Retries 429 and 5xx only. Fails loudly on 400, 402 and 422
- [x] T0.5 Budget guard: running token totals per model, hard stop at a configured cap (Muse $250 credits, SubQ 100M tokens)
- [x] T0.6 Prompt firewall: a single function every prompt passes through that rejects any text containing a path under `services/` or any string from the sealed files
- [x] T0.7 Tests with recorded fixtures (no live calls in CI)

**Done when:** one call each to Muse and SubQ goes through the wrappers, is counted in the budget guard, and a prompt containing `services/` is refused.

## T1. Sandbox and tool surface

- [x] T1.1 `sandbox/digsite_fs.py`: read-only file API rooted at `dig-site/`. Rejects `..`, symlinks out, and the whole of `bin/`
- [x] T1.2 `sandbox/binary.py`: `start_binary()` and `send_request()` using `BlackBoxClient(command=[python3.12, <abs path>/pressure.pyz])`. Handles process lifetime, timeouts and crash recovery. Never exposes the file bytes
- [x] T1.3 Binary metadata only: size, SHA-256, entry point from `MANIFEST.json`, `strings`-style inspection limited to what the dig-site rules allow
- [x] T1.4 Tests: path escape attempts fail, `bin/` read fails, `services/` unreachable, binary replies match the contract

**Done when:** an agent tool call can play one full game through the binary and cannot read anything outside `dig-site/`.

## T2. Evidence ledger (with source and tier from the start)

- [x] T2.1 Claim schema: `id`, `text`, `rule` (optional executable predicate), `source` (`manual`, `code`, `binary`, `trace`, `model`), `tier` (`documented`, `read_from_code`, `observed`, `inferred`), `status` (Observed, Inferred, Confirmed, Refuted, Open), `evidence` (trace IDs), `experiment_ids`, `created_by`, timestamps
- [x] T2.2 Finding schema: `claim_id`, `artifact`, `artifact_said`, `traces_said`, `trace_ids`, `kind` (`stale`, `wrong`, `incomplete`, `missing`)
- [x] T2.3 Enforced transitions in code: only the scorer module may set Confirmed or Refuted, and only with trace IDs. Confirmed falls back to Inferred if not retested in a new situation
- [x] T2.4 Prediction log: predictions are written before an action runs and are append-only
- [x] T2.5 Persistence: JSONL on disk under `runs/<run_id>/`, resume from saved state
- [x] T2.6 Query API for the Coordinator: by status, by tier, by source, open questions
- [x] T2.7 Tests: a claim with no trace ID is rejected, a manual claim cannot become Confirmed without traces, a model call cannot set a status

**Done when:** the ledger rejects every illegal transition in tests and survives a restart.

## T3. Survey (no games played)

Sub-agent: Surveyor.

- [x] T3.1 Parse the manual into Documented-tier claims, for example "vent lowers pressure by 3", "hold up to 7 cards", "scan shows 3 cards", "game ends when one player remains", "limit is 12"
- [x] T3.2 Parse the partial code into Read-from-code claims, deterministically with `ast`; optional `--use-muse` pass adds more (OPEN, created_by muse)
- [x] T3.3 Extract **unknowns** from the code: hidden state (`direction`, `jammed`, `extra_actions`, `skip_next`), parameters whose values are absent (`hand_limit`, `scan_depth`, the pressure limit), imports with no source (`rules.py`), and functions with lost bodies (`_gear`, `_vent`, `_reverse`, `_double`, `_jam`, `_draw`)
- [x] T3.4 Flag hints the manual never mentions, such as `_is_stalemate`, and add them as Open claims
- [x] T3.5 Binary entry: hash, size and entry point only
- [x] T3.6 Produce a **ranked probe list**: missing functions and unknown parameters first, hint-only behaviors second, documented claims last
- [x] T3.7 Tests: the survey adds zero Confirmed claims, every claim has a source and tier, the probe list ranks missing functions first

**Done when:** `python -m surveyor.main.survey --run-id r-01` (run from `agent/`) writes a starting ledger, `probe_list.json` and `survey.json`, and plays no game.

## T4. Check claims against the seed corpus

Sub-agent: Auditor.

- [x] T4.1 Corpus loader with the `train` and `heldout` split enforced: `FittingCorpus` is built from train games only, so held-out steps do not exist for fitting code
- [x] T4.2 Deterministic claim checkers for numeric and structural claims (pressure deltas per card, cards revealed by scan, maximum hand size when draw is offered, `game_over` with a null winner)
- [x] T4.3 Refuting a documented or code claim creates a **finding** with the trace ID. Nothing is deleted
- [x] T4.4 SubQ surprise triage: label each unexplained step with the claim it bears on
- [x] T4.5 Claims the corpus cannot decide stay Open and are added to the probe list
- [x] T4.6 Tests against the real corpus: the stale manual claims are found as findings, with trace IDs that exist

**Done when:** the ledger shows which manual and code claims the corpus refutes, which it supports, and which remain open, all without a new probe.

## T5. Prober and targeted experiments

Sub-agent: Prober.

- [x] T5.1 Game runner over the binary: stacked-deck scripts, step recording with trace IDs, `prediction` and `surprise` filled in
- [x] T5.2 Predict-then-check: query the current ledger rules for a prediction before each action, log it, then act
- [x] T5.3 Experiment designer (Muse): for competing hypotheses, produce a script where they predict different outcomes
- [x] T5.4 Experiment selector (SubQ): pick the most informative experiment from the candidates, given the ledger and surprise log
- [x] T5.5 Seed experiment set from the probe list: jam then draw, two doubles in a row, reverse with 3 and 4 players, overflow aftermath, stalemate by emptying hands and the pile, hand limit, scan depth
- [x] T5.6 Probe budget counter (games and steps) so the ablation can compare probe counts
- [x] T5.7 Tests with a fake binary, plus one live test against the real binary

**Done when:** each seed experiment runs against the binary and its traces are stored with IDs and predictions.

## T6. Hypothesize and score

Sub-agent: Hypothesizer.

- [x] T6.1 Muse proposes rules as small executable functions `predict(view, action) -> events`, with competing alternatives per behavior
- [x] T6.2 Rule runner in the model-code sandbox (T9 dependency): timeout, no keys, no network
- [x] T6.3 Scorer (pure code): runs rules on train and held-out traces and is the only writer of Confirmed and Refuted
- [x] T6.4 Confirmation rule: no mismatch anywhere, >=3 held-out steps, a separating step, and a fresh probe step. Survivors that predict identically on every step are one hypothesis: one representative is confirmed, but only after a separation attempt
- [x] T6.5 Loop: probe, hypothesize, test, repeat until no Open probe-list items remain or the budget ends
- [x] T6.6 SubQ claim audit: for each ledger claim, read its cited traces and label supported, unsupported or ambiguous. Disagreements go to a human-visible list, not to the status
- [x] T6.7 Tests: a wrong rule cannot be Confirmed, the scorer refuses held-out use for fitting

**Done when:** the loop confirms at least one rule that differs from the manual and cites its experiment and held-out traces.

## T7. Rebuild in JavaScript

Sub-agent: Rebuilder.

- [x] T7.1 Rebuild contract: JSON-lines on stdin and stdout, same methods, events and errors as the service contract, so one client drives both
- [x] T7.2 Muse writes the engine from **Confirmed rules only**, using the partial code's structure (`_finish_action`, `_advance_turn`) as a skeleton
- [x] T7.3 Unconfirmed behavior is not guessed: it is left as an explicit "unknown" branch and listed in the report
- [x] T7.4 Repair loop: takes a counterexample from the Verifier and asks Muse for a fix, capped at N attempts
- [x] T7.5 Tests: the rebuild answers every contract method, and passes a hand-written smoke game

**Done when:** `node rebuild/engine.js` speaks the contract and plays a full game.

## T8. Verifier

Sub-agent: Verifier. No model calls.

- [x] T8.1 Side-by-side driver using `BlackBoxClient(command=...)` for the binary and the rebuild
- [x] T8.2 Replay every recorded trace, events and views compared exactly
- [x] T8.3 Fuzz: thousands of random scripted games with random legal actions on both, compared step by step
- [x] T8.4 Shrinker: reduce any divergence to the shortest failing game
- [x] T8.5 Gate: returns Accepted or Blocked with the counterexample. Accepted needs zero divergences and at least 5,000 fuzzed games
- [x] T8.6 Does not read the manual or the partial code
- [x] T8.7 Tests: a deliberately broken rebuild is Blocked with a short counterexample

**Done when:** the Verifier blocks a broken rebuild and accepts a correct one.

## T9. Safe execution of model code

- [x] T9.1 Subprocess runner with CPU and wall-clock limits, a scrubbed environment (no API keys), an import allow-list. Best effort, not a security boundary: it cannot stop a determined attacker
- [x] T9.2 Used for Hypothesizer rules and for the JavaScript rebuild during fuzzing
- [x] T9.3 Tests: a rule that reads `META_API_KEY` sees nothing, an infinite loop is killed

## T10. Local coordinator

Sub-agent: Coordinator. Everything runs locally on the user's machine. There is no Agentverse or ASI:One. The agent is used from the terminal or from the UI app that sits on top of it. (Former T10.1 and T10.6 were removed with the hosted setup. The remaining numbers are kept so other tasks still point to the right place.)

- [x] T10.2 SubQ chat routing: start run, status, ledger, open questions, explain a claim, show rebuild, show report
- [x] T10.3 Immediate acknowledgement, background run, progress messages
- [x] T10.4 Answers come from ledger queries and never from a free model guess about what is true
- [x] T10.5 Reliability: retry model calls, fall back to the last good ledger, resume from saved traces

## T11. Report

- [ ] T11.1 Sections: Observed, Confirmed, Inferred and Open, Equivalence, Mutation check
- [ ] T11.2 **Where the evidence was wrong**: table of artifact claims with source, verdict and trace ID (for example Manual, "Vent lowers pressure by 3", Refuted, stale, trace ID)
- [ ] T11.3 Mutation check: reveal the planted mutations from `services/pressure/sealed/runs/` **only in the report step, after the run**, never in a prompt
- [ ] T11.4 Every claim in the report cites trace IDs, and the build fails if one does not

## T12. Controls and ablation

- [ ] T12.1 Ablation: traces only vs traces plus artifacts. Same Confirmed rules expected, fewer probes with artifacts
- [ ] T12.2 Opaque-token run (`--opaque`) reaches the same ledger
- [ ] T12.3 Zero-probe baseline scores low, with a learning curve over probe count

## T13. Docs and submission

- [ ] T13.1 Update the top-level `README.md`: it still describes Exploding Kittens, but the built black box is Pressure
- [ ] T13.2 Run and test instructions, public repo check, confirm `.env`, `sealed/runs/` and `dig-site/bin/` are ignored
- [ ] T13.3 Demo rehearsal running locally (terminal and the UI app), including a replay of a saved run as a fallback

## T14. One-line requests and plain-language answers

Sub-agent: Concierge. The user types a single line. The backend does the rest, live.

Examples of what a user can type:
- "Rebuild the Pressure game in JS" or "rebuild it in Go" (any language is accepted)
- "Explain the rules of the game"
- "What did you find wrong in the manual?"
- "How far along is the rebuild?"

- [ ] T14.1 Intent step: Muse reads the one-liner and picks one of: `rebuild` (with a target language), `explain` (a question about the service), `status`, or `out_of_scope`. SubQ `choice()` may pre-route obvious cases, but Muse makes the final call
- [ ] T14.2 Scope guard: only questions about the service being built are answered (its rules, claims, findings, traces, rebuild, run status). Everything else gets one polite line saying so. User text and trace contents are treated as untrusted and go through the prompt firewall (T0.6). Nothing from `services/` can appear in an answer before the T11.3 reveal
- [ ] T14.3 Target language config: turn the language named in the one-liner into `{name, build_command, run_command, file_layout, prompt_hints}` (JS and Go ship first). If the toolchain is not installed, say so plainly and do not fall back silently. Output goes to `rebuild/<lang>/`
- [ ] T14.4 Rebuild trigger: a `rebuild` request starts the full loop (survey, probe, hypothesize, rebuild, verify) as a background run on the chosen language, acknowledges immediately, and streams progress through T15
- [ ] T14.5 Answer retrieval: pull the relevant claims, findings and trace IDs from the ledger first. Long-context reading (SubQ, or Muse if SubQ cannot do it) is used to find evidence, not to remember things. The ledger on disk stays the single memory
- [ ] T14.6 Answer writing: Muse (the best available model) writes a short, plain answer from the retrieved evidence only. Rules for the style: 2 to 4 sentences, no jargon, no internal names like "tier" or "ledger" unless the user asks, one trace ID cited at the end for each fact. If the evidence is missing, the answer says "not known yet" and does not guess
- [ ] T14.7 Honest uncertainty: answers say whether a rule is Confirmed, Inferred or still Open, in everyday words ("proven by testing", "likely, not yet proven", "still unknown")
- [ ] T14.8 Tests: an off-topic question is declined, an `explain` answer cites existing trace IDs, a question about an unconfirmed rule is answered as "not known yet", a prompt that tries to pull `services/` content is refused, "rebuild in Go" with no Go installed returns a clear message

**Done when:** typing "rebuild it in Go" starts a run, typing "explain the rules" returns a short plain answer that cites real traces, and typing an unrelated question gets a polite decline.

## T15. Live action feed

Sub-agent: Live feed. No model calls, so every line is true to what actually happened.

Each loop of the agent produces exactly one line saying what it just did, then the next loop produces the next line, and so on.

Example lines:
- "Read the manual: found 14 claims to check."
- "Played a game with a jammed deck: the manual said blocked, the game allowed it."
- "Marked 'vent lowers pressure by 3' as wrong (trace 0412)."
- "Confirmed 'hand limit is 7' on 8 held-out games."
- "Rebuilt the engine in Go: 3 behaviors still unknown."
- "Checked 3,200 of 5,000 games: 0 differences so far."

- [ ] T15.1 Event log: every agent action (survey, probe, score, rebuild, verify, repair) appends one structured event to `runs/<run_id>/events.jsonl` (append-only, with timestamp, loop number and trace IDs)
- [ ] T15.2 Line writer: a fixed template per event type turns an event into one plain sentence. Templates, not a model, so the line cannot say something that did not happen. Max one line per loop, with a short loop counter
- [ ] T15.3 Live stream: the feed is pushed to the local chat as each loop finishes (T10.3) and can also be tailed from the terminal, so the demo shows the agent working in real time
- [ ] T15.4 Next-step line: after each action, one line says what happens next ("Next: test two doubles in a row"), taken from the probe list and loop state
- [ ] T15.5 Safe by default: lines never include anything from `services/`, API keys or raw model output. The same firewall checks apply
- [ ] T15.6 Replay: `events.jsonl` can be replayed at speed, so the demo can be rehearsed or recovered if live calls are slow
- [ ] T15.7 Tests: every event type has a template, a line never exceeds one sentence, a forbidden string in an event is blocked, replay produces the same lines in the same order

**Done when:** a live run shows one clear line per loop in the chat, in order, and the same run can be replayed from disk.

---

## Order of work

1. T0 and T1 (foundations)
2. T2 (ledger)
3. T3 then T4 (survey, then corpus check)
4. T5 and T6 (probe and hypothesize loop)
5. T9 alongside T6
6. T7 then T8 (rebuild, verify)
7. T15.1 and T15.2 early (so every later task logs events from the start)
8. T10.2 to T10.5, T14, T15.3 to T15.7
9. T11, T12, T13

## Open decisions

- Python version: the binary needs 3.12 and `pydantic>=2.7`. The agent should use the same interpreter.
- Whether the held-out split is re-cut for new probe games, or only the corpus split is used.
