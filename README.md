# Strata

> Dig through a black box layer by layer, rebuild it in a new language, and prove the rebuild behaves the same.

Strata is a multi-agent software archaeology system that you talk to in ASI:One. It studies a running Exploding Kittens game as a black box and never reads its source. It plays thousands of scripted games, records every step as evidence, and keeps a ledger that separates what it observed from what it inferred. It then rebuilds the game in JavaScript and proves the rebuild matches the original, game by game.

---

## 1. Why this matters

Many critical programs outlive their source code, their documentation, or the people who wrote them. Often the program's behavior is the only evidence left. Strata shows how an agent can recover that behavior and move it to a new runtime. It also says exactly what it proved, what it guessed, and what it never tested.

Exploding Kittens stands in for that legacy system. The game is small enough to finish in a day. It still has hidden state, chained card effects, and randomness, which are the same things that make real migrations risky.

---

## 2. Hackathon fit

- **Track:** Fetch.ai, "Agents That Prove Their Work"
- **Primary theme:** Agents that reconstruct the past
- **Secondary theme:** Agents that verify before they trust. The Verifier blocks any rebuild that does not match the original.

| Problem statement | How Strata answers it |
|---|---|
| Recover authorized software | Our own Python implementation of Exploding Kittens, run as a black box |
| Incomplete evidence such as runtime traces | Every step of every probe game is saved as a trace with an ID |
| Map an unfamiliar program | Probe loop with predictions and a surprise log |
| Track uncertain hypotheses | Evidence ledger with five statuses |
| Separate observed from inferred | Every claim is labeled and cites trace IDs |
| Rebuild or port | JavaScript rebuild generated from confirmed rules only |
| Prove behavioral equivalence across languages and runtimes | Python original vs. JavaScript (Node) rebuild, compared by replay and fuzzing |

---

## 3. Technology

| Layer | Choice | Role |
|---|---|---|
| Reasoning and code | **Meta Muse Spark** (Meta Model API) | Writes hypotheses, experiments, the rebuild, and the report |
| Long-context decisions | **SubQ Snap 1.1** (Subquadratic) | Reads the full evidence and makes structured choices |
| Agents | **Fetch.ai uAgents** with the **Agent Chat Protocol** | Agent runtime and the message format ASI:One uses |
| Discovery and chat | **Agentverse** and **ASI:One** | Agents are registered on Agentverse; users talk to them in ASI:One |
| Ground truth | Deterministic Python and Node code | Scoring, replay, fuzzing, and the pass/fail decision |

**Principle:** models propose and choose; code decides what is true. No model output can mark a rule Confirmed or a rebuild Accepted.

### Meta Muse Spark: open-ended work

- Proposes hypotheses as small executable rules, such as `predict(view, action) -> events`.
- Designs separating experiments: scripted games where competing hypotheses predict different outcomes.
- Writes the JavaScript rebuild from Confirmed rules only.
- Repairs the rebuild when the Verifier returns a counterexample.
- Writes the plain-language parts of the report.

Accessed through the Meta Model API, which is OpenAI SDK-compatible. Budget: $250 in credits.

### SubQ Snap 1.1: long-context decisions

SubQ Snap is a decision model. It returns a choice, a score, or a label instead of open-ended text, and it has a 384K-token context window. That fits the points where Strata has to choose while looking at all the evidence at once:

- **Next experiment:** given the full ledger, the open hypotheses, and the surprise log, pick the most informative experiment from Muse's candidates.
- **Surprise triage:** label each failed prediction with the hypothesis it bears on.
- **Claim audit:** for each ledger claim, read its cited traces and label it supported, unsupported, or ambiguous. This catches claims whose wording does not match their evidence.
- **Chat routing:** classify each ASI:One message, for example start a run, show status, show the ledger, explain a claim, or show the report.

Budget: 100M free tokens from console.subq.ai.

Using two model families is deliberate. Muse writes the claims, a different model audits them, and neither one decides what is true.

---

## 4. The black box: Exploding Kittens

We write the original game ourselves in Python. The agents reach it only through the interface below. Agent prompts never include this README, the rules, or the source code.

### Rules of the original (hidden from the agents)

These are the rules our black box implements, for 2 or 3 players:

- **Setup:** each player is dealt 4 cards plus 1 Defuse. Exploding Kittens (one fewer than the number of players) and the spare Defuse cards are mixed into the draw pile.
- **Turn:** play any number of cards, then draw one card. Drawing ends your turn.
- **Exploding Kitten:** if you draw one and cannot defuse it, you are out.
- **Defuse:** saves you from an Exploding Kitten. You then put the kitten back into the draw pile at a position you choose.
- **Attack:** end your turn without drawing. The next player takes 2 turns. If that player plays Attack, their turns end and the following player takes 2 turns.
- **Skip:** end one turn without drawing. Under an Attack, it ends only one of the two turns.
- **Favor:** a player of your choice gives you a card of their choice.
- **Shuffle:** shuffle the draw pile.
- **See the Future:** look at the top 3 cards of the draw pile.
- **Cat cards:** play a matching pair to steal a random card from a player of your choice.
- **End:** the last player left wins.

Nope is out of scope, because it needs reaction windows between players.

### Hidden mutations

Each run turns on 1 to 3 rule changes, chosen by seed. The agents are not told which ones are active.

| Mutation | Effect |
|---|---|
| Even-turn Defuse | Defuse only works on even-numbered turns |
| Kitten to top | A defused kitten always goes on top, whatever position was chosen |
| Stacking Attack | The next player takes their remaining turns plus 2 |
| Short future | See the Future shows 2 cards instead of 3 |
| Big Skip | Skip ends all remaining turns, not just one |
| Left steal | A cat pair always steals from the player on your left |

A model that recites the real rules gets these wrong. Recovering them is how we show the rules came from evidence.

### Interface the agents get

| Call | Returns |
|---|---|
| `new_game(players, script)` | A game ID and the first view |
| `legal_actions(game_id)` | The actions allowed right now |
| `act(game_id, action)` | The events caused by the action, and the new view |
| `view(game_id)` | The current visible state |

The view shows every hand (the Prober plays all seats), the discard pile, the size of the draw pile, whose turn it is, and how many turns they have left. It never shows the order of the draw pile.

### Scripted randomness

In test mode, every random outcome comes from a script supplied with the game. This covers the starting deck order, each Shuffle result, and the card taken by each random steal. The original and the rebuild read the same script, so their results can be compared exactly. A free-running random number generator, compared statistically, is a stretch goal.

### What the agents must discover

- How the game is set up: hand size, Defuse dealing, and the number of kittens
- What each card does, including the mutated ones
- How turns pass, especially under Attack and Skip
- Where a defused kitten goes
- When a player is out and when the game ends
- How effects combine, for example Attack then Skip, Favor on a player with no cards, or Shuffle after See the Future

### Trace format

Each step is one JSON line:

```json
{"trace_id": "t-001180", "run_id": "r-07", "game_id": "g-0042", "step": 9,
 "seat": 1, "turn": 3, "action": {"play": "Defuse"},
 "prediction": {"seat_1_out": false},
 "events": [{"type": "eliminated", "seat": 1}],
 "surprise": true}
```

---

## 5. How Strata works

```
   PROBE  →  HYPOTHESIZE  →  TEST  →  REBUILD  →  VERIFY
     ↑                         │                    │
     └──── surprises ──────────┘   counterexamples ─┘ (back to REBUILD)
```

1. **Probe.** The Prober plays scripted games against the black box. Before each action, it records what the current ledger predicts. A wrong prediction is a surprise, and surprises steer the next probes.
2. **Hypothesize.** Muse reads the evidence and proposes rules as small executable functions. Each behavior gets competing alternatives, for example "Attack gives the next player 2 turns" vs. "Attack adds 2 to their remaining turns".
3. **Test.** Muse designs scripts where the alternatives predict different outcomes, and SubQ picks which experiment to run next. The engine runs it and scores every hypothesis against held-out traces.
4. **Rebuild.** Muse writes the JavaScript game from Confirmed rules only.
5. **Verify.** The Verifier replays every trace on both versions and fuzzes thousands of new scripted games. It shrinks any divergence to the shortest failing game and sends it back to step 4. The rebuild is accepted only with zero divergences.

---

## 6. Evidence ledger

| Status | Meaning |
|---|---|
| **Observed** | Seen directly in a trace, with no interpretation |
| **Inferred** | A rule that fits the observations but has not beaten its alternatives yet |
| **Confirmed** | Predicts held-out traces and won a separating experiment |
| **Refuted** | At least one trace contradicts it. Kept, with that trace ID. |
| **Open** | Never tested |

Ledger rules:

- A claim without a trace ID is rejected.
- A Confirmed rule that is not retested in new situations falls back to Inferred.
- Every prediction is logged before the action runs, so surprises cannot be rewritten afterward.

Example entries:

| Claim | Status | Evidence |
|---|---|---|
| Seat 1 drew an Exploding Kitten while holding a Defuse and stayed in | Observed | t-000412 |
| Defuse prevents elimination | Inferred | 37 traces |
| Defuse prevents elimination on every turn | Refuted | t-001180 (turn 3, seat 1 eliminated) |
| Defuse prevents elimination on even turns only | Confirmed | Experiment e-05, 214 held-out traces |
| Favor on a player with an empty hand | Open | Never tested |

---

## 7. Proving it learned instead of recalled

Muse already knows Exploding Kittens, so Strata includes controls:

| Control | What it shows |
|---|---|
| Hidden mutations | Reciting the real rules fails, so correct mutated rules must come from evidence |
| Opaque-token run | The same run with cards and actions renamed `c1..cN` and `a1..aK` reaches the same ledger |
| Predict-then-check | Predictions are logged before each action, and surprises show the evidence correcting the model |
| Held-out traces | Rules must predict traces they were not built from |
| Zero-probe ablation | A rebuild made with no probes scores low, and the score rises with more probes |

The claim we make: the accepted rules are determined by the traces, not by recall. We cannot show the model had zero influence on which hypotheses it thought of first.

---

## 8. Proving equivalence

- **Different language and runtime:** the original is Python and the rebuild is JavaScript on Node.
- **Replay:** every recorded trace runs on the rebuild. Events and views must match exactly.
- **Fuzz:** thousands of new scripted games run on both versions and are compared step by step.
- **Shrink:** any divergence is reduced to the shortest game that still fails.
- **Gate:** the Verifier returns **Accepted** or **Blocked**. A Blocked result includes the shortest counterexample.
- **Independence:** the Verifier is a separate agent and never uses a model to decide pass or fail.

### Evidence report

1. **Observed:** raw facts, with trace counts.
2. **Confirmed:** each rule, with the experiment and held-out traces that confirmed it.
3. **Inferred and Open:** what is still a guess, and what was never tested.
4. **Equivalence:** replay and fuzz results, known differences, and any counterexample.
5. **Mutation check:** the planted mutations next to the ones Strata found.

---

## 9. Agents and deployment

| Agent | Job | Uses |
|---|---|---|
| **Strata Coordinator** | ASI:One entry point. Talks to the user, starts runs, posts progress, and answers questions about the ledger. | uAgents, Agent Chat Protocol, SubQ for routing |
| **Prober** | Plays scripted games against the black box and records traces | Black box interface |
| **Hypothesizer** | Proposes rules and experiments | Muse, SubQ |
| **Rebuilder** | Writes and repairs the JavaScript rebuild | Muse |
| **Verifier** | Replays, fuzzes, shrinks, and returns Accepted or Blocked | Python and Node, no model |

- **Minimum for eligibility:** the Coordinator is registered on Agentverse and implements the Agent Chat Protocol.
- **Target:** the Coordinator and the Verifier are separate registered agents, so verification is independent. The other roles can start as modules inside the Coordinator and be split out later.

### Where things run

- The agents run on our machine and connect to Agentverse through a mailbox, so ASI:One can reach them.
- The black box runs as a separate process. The agents talk to it only through the interface.
- Model-generated code (hypotheses and the rebuild) runs in a separate process with a time limit and no API keys in its environment.
- API keys live in a local `.env` file, which is never committed.

### Long runs in a chat

The Coordinator acknowledges each message immediately, posts progress during a run, and sends the report at the end. The chat never waits on one long call.

### Using Strata in ASI:One

| You say | Strata does |
|---|---|
| "Reconstruct the Exploding Kittens black box" | Starts a run and posts progress |
| "Status" | Shows games played and claims by status |
| "Show the ledger" or "Show open questions" | Returns ledger entries |
| "Why do you think Defuse fails on odd turns?" | Returns the claim with its trace IDs and experiment |
| "Show the rebuild" | Returns the JavaScript |
| "Show the report" | Returns the evidence report |

### Bonus features

- **Interactive Cards:** a ledger table, a review card to mark a discovered quirk as "preserve" or "fix", and a final report card.
- **Reliability:** retry failed model calls, fall back to the last good ledger, and resume a run from saved traces.
- **Payment Protocol (stretch):** charge per reconstruction run.

---

## 10. Scope

**Must have**
- [ ] Black box with the base rules, hidden mutations, scripted randomness, and the interface
- [ ] Trace recording with trace IDs
- [ ] Ledger with the five statuses and the citation rule
- [ ] Prober with predict-then-check and a surprise log
- [ ] Muse hypotheses and experiments, scored on held-out traces
- [ ] SubQ experiment selection and claim audit
- [ ] Muse JavaScript rebuild from Confirmed rules
- [ ] Verifier: replay, fuzz, shrink, Accepted or Blocked
- [ ] Coordinator on Agentverse with the Agent Chat Protocol, usable end to end in ASI:One
- [ ] Evidence report, including the mutation check

**Bonus, if time allows**
- [ ] Verifier as a separate registered agent
- [ ] Interactive Cards for the ledger and the report
- [ ] Zero-probe ablation and learning curve
- [ ] Opaque-token control run
- [ ] Preserve-or-fix choice for discovered quirks

**Stretch**
- [ ] Nope card
- [ ] Free-running randomness with statistical comparison
- [ ] Payment Protocol

**Out of scope**
- Bang! The Dice Game, a second rebuild language, voice or video features, and a custom frontend

---

## 11. Six-hour plan

| Time | Milestone |
|---|---|
| 0:00-0:30 | Hello-world agent on Agentverse, reachable from ASI:One. Muse and SubQ keys tested. |
| 0:30-1:30 | Black box: rules, mutations, scripted randomness, interface, trace recording |
| 1:30-2:30 | Ledger, Prober with predictions and surprises, held-out scorer |
| 2:30-3:30 | Muse hypotheses and experiments; SubQ experiment selection and claim audit |
| 3:30-4:30 | Muse JavaScript rebuild; Verifier replay, fuzz, shrink, and gate |
| 4:30-5:15 | Full flow in ASI:One: progress messages, ledger, report |
| 5:15-5:45 | Error handling, run instructions, public repo check |
| 5:45-6:00 | Demo rehearsal |

The ASI:One connection comes first because it is the biggest eligibility risk.

---

## 12. Demo, entirely inside ASI:One

1. Ask Strata to reconstruct the Exploding Kittens black box.
2. Strata confirms the run and posts progress: games played and claims by status.
3. Strata predicts standard behavior, a Defuse fails on turn 3, and the surprise appears in the chat.
4. Strata tests the competing explanations and reports the winner with trace IDs.
5. If a rebuild fails, the Verifier blocks it with the shortest counterexample and the Rebuilder repairs it.
6. The Verifier accepts the rebuild, for example "5,000 games, 0 divergences".
7. Strata shows the report, and we reveal the planted mutations next to the ones it found.

---

## 13. Success criteria

- The complete workflow runs inside an ASI:One conversation.
- The rebuild passes 100% of recorded traces and shows 0 divergences across at least 5,000 fuzzed games.
- Strata recovers each planted mutation, or the report lists it as missed.
- At least one surprise leads to a Confirmed mutated rule.
- Every claim in the report cites trace IDs.

---

## 14. Honest limits

- Equivalence covers what was tested. Untested combinations stay Open.
- Exact equivalence assumes scripted randomness.
- The model's prior knowledge cannot be removed. The controls show it is not enough, not that it is absent.
- The black box is our own code. "Without seeing the code" describes the agents' access, not secrecy.
- Real card names make recall easier. The mutations and the opaque-token run are the counter to that.

---

## 15. Setup

Run commands will be added as the code lands.

**Prerequisites:** Python 3.11+ and Node 20+.

| Credential | Used for | Where to get it |
|---|---|---|
| Meta Model API key | Muse Spark | Meta Model API |
| SubQ API key | SubQ Snap 1.1 | console.subq.ai |
| Agentverse account | Registering agents and the mailbox | agentverse.ai |
| ASI:One account | Testing and the demo | asi1.ai |
| Agent seed phrase | Each agent's identity | Generated locally and kept secret |

Keep every key and seed phrase in `.env`. Never commit them.

---

## 16. Submission checklist

- [ ] At least one agent registered on Agentverse
- [ ] Agent Chat Protocol implemented
- [ ] Agent discoverable and usable through ASI:One
- [ ] Primary workflow completed entirely in an ASI:One conversation
- [ ] Meaningful tool execution or multi-agent orchestration shown
- [ ] Public GitHub repository with run and test instructions
- [ ] Submitted on the Cerebral Valley dashboard
- [ ] Registered through the AIE Summit ASI:One Submission Agent
