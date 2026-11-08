"""T7: write the JavaScript rebuild from Confirmed rules only, check it, repair it.

Muse writes `engine.js`; `server.js` (fixed, not model-written) wraps it in the same
JSON-lines contract the original speaks, so the Verifier drives both with one client.
Behavior no Confirmed rule covers is left as an explicit unknown that throws.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from auditor.main.corpus import TraceStore
from auditor.main.digest import step_line
from hypothesizer.main.groups import GROUPS
from .languages import JAVASCRIPT, LanguageConfig, LanguageError
from livefeed.main.events import EventLog, safe_emit
from llm.main.muse import MuseClient
from sandbox.main.binary import BinaryError, BinaryProcess, BinaryRequestError

HERE = Path(__file__).resolve().parent
SERVER_TEMPLATE = HERE / "server.js"

SYSTEM = (
    "You port a small game program to JavaScript (Node 20, no dependencies) from a specification "
    "of verified rules. You never invent behavior: anything the rules do not establish must be an "
    "explicit unknown that throws. Correctness against recorded behavior is all that matters."
)

ENGINE_API = JAVASCRIPT.engine_api


class RebuildError(LanguageError):
    pass


@dataclass
class RebuildResult:
    directory: Path
    engine_js: str                              # the engine source, in whichever language was asked for
    unknown_behaviors: list[str] = field(default_factory=list)
    syntax_ok: bool = False
    smoke_ok: bool = False
    problems: list[str] = field(default_factory=list)
    language: LanguageConfig = JAVASCRIPT

    @property
    def command(self) -> list[str]:
        return self.language.command(self.directory)

    @property
    def ready(self) -> bool:
        return self.syntax_ok and self.smoke_ok


def extract_js(text: str) -> str:
    """Take the JavaScript out of a model reply (fenced block preferred)."""
    blocks = re.findall(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)```", text, re.DOTALL)
    code = max(blocks, key=len) if blocks else text
    code = code.strip()
    if "module.exports" not in code and "exports." not in code:
        raise RebuildError("the reply did not contain a CommonJS module (no module.exports)")
    return code + "\n"


def node_check(path: Path) -> str | None:
    """Return None if `node --check` accepts the file, else the error text."""
    done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    return None if done.returncode == 0 else done.stderr.strip()[:600]


def write_rebuild(directory: Path, engine_js: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "engine.js").write_text(engine_js)
    shutil.copyfile(SERVER_TEMPLATE, directory / "server.js")
    return directory


def unknown_behaviors_of(directory: Path) -> list[str]:
    script = ("const e=require(process.argv[1]);"
              "process.stdout.write(JSON.stringify(e.UNKNOWN_BEHAVIORS||[]))")
    done = subprocess.run(["node", "-e", script, str(directory / "engine.js")],
                          capture_output=True, text=True, timeout=30)
    try:
        return [str(x) for x in json.loads(done.stdout)]
    except json.JSONDecodeError:
        return []


def smoke_game(command: list[str], deck: list[str], players: int = 2, max_steps: int = 60) -> str | None:
    """Play one game through the contract. Returns None if every reply is well formed."""
    try:
        with BinaryProcess(command=command, timeout=10) as box:
            started = box.new_game(players, {"deck": deck})
            view = started["view"]
            for key in ("players", "hands", "alive", "discard", "draw_pile_size", "current_seat",
                        "actions_left", "pressure", "turn", "over", "winner"):
                if key not in view:
                    return f"initial view lacks '{key}'"
            gid = started["game_id"]
            for _ in range(max_steps):
                if view["over"]:
                    break
                legal = box.legal_actions(gid)
                if not isinstance(legal, list) or not legal:
                    return "no legal actions in a game that is not over"
                reply = box.act(gid, legal[0])
                if not isinstance(reply.get("events"), list) or "view" not in reply:
                    return "act must return {events, view}"
                view = reply["view"]
            try:
                box.act(gid, {"type": "play", "card": "no-such-card"})
                return "an illegal action was accepted"
            except BinaryRequestError as error:
                if error.kind not in ("illegal_action", "invalid_request"):
                    return f"illegal action gave error kind {error.kind}"
    except BinaryRequestError as error:
        return f"{error.kind}: {error.message}"
    except BinaryError as error:
        return f"{type(error).__name__}: {error}"
    return None


class Rebuilder:
    def __init__(self, muse: MuseClient, store: TraceStore, *, lead_code: str = "",
                 language: LanguageConfig = JAVASCRIPT):
        self.muse, self.store, self.lead_code = muse, store, lead_code
        self.language = language

    def _extract(self, reply: str) -> str:
        if self.language is JAVASCRIPT:
            return extract_js(reply)
        try:
            return self.language.extract(reply)
        except LanguageError as error:
            raise RebuildError(str(error)) from error

    # ---------- facts the prompt may state (observed in the fitting data) ----------
    def _facts(self) -> dict[str, Any]:
        games = self.store.fitting().games()
        decks = Counter(tuple(sorted(Counter(g.script["deck"]).items())) for g in games
                        if len(g.script["deck"]) >= 40)
        deck_counts = dict(decks.most_common(1)[0][0]) if decks else {}
        cards = sorted({c for g in games for c in g.script["deck"]})
        return {"cards": cards, "deck_counts": deck_counts,
                "hand_size": len(games[0].initial_view["hands"][0]) if games else None}

    def _examples(self) -> str:
        fitting = self.store.fitting().games()
        lines = []
        for game in fitting[:2]:
            lines.append(f"initial view ({game.players} players): {json.dumps(game.initial_view)}")
        interesting = [s for g in fitting for s in g.steps
                       if any(e["type"] in ("eliminated", "revealed", "game_over", "passed")
                              for e in s["events"])][:5]
        plain = [s for g in fitting[:1] for s in g.steps[:3]]
        for step in plain + interesting:
            lines.append(json.dumps({k: step[k] for k in ("action", "legal", "events", "before", "view")}))
        return "\n".join(lines)

    def build_prompt(self, rules: list[dict[str, Any]]) -> str:
        covered = {r["group"] for r in rules}
        missing = [g for g in GROUPS if g not in covered]
        facts = self._facts()
        parts = [self.language.engine_api,
                 f"FACTS observed in recorded games: cards = {facts['cards']}; a full deck holds "
                 f"{sum(facts['deck_counts'].values())} cards: {facts['deck_counts']}; "
                 f"{facts['hand_size']} cards are dealt to each player.",
                 "CONFIRMED RULES (each is a Python predictor verified against recorded runs; "
                 "port the behavior it establishes):\n" + "\n\n".join(
                     f"### [{r['group']}] {r['text']}\n(evidence: {', '.join(r['evidence'][:4])})\n"
                     f"```python\n{r['code']}\n```" for r in rules)]
        if missing:
            parts.append("GROUPS WITH NO CONFIRMED RULE (behavior unknown; do NOT guess): "
                         + ", ".join(missing) + ". Where the engine would need such behavior, call "
                         "unknown('...') and list it in UNKNOWN_BEHAVIORS.")
        parts.append("RECORDED EXAMPLES (shapes of views, events and legal action lists):\n" + self._examples())
        if self.lead_code:
            parts.append("RECOVERED SOURCE FRAGMENT: structure only (turn flow in _finish_action and "
                         "_advance_turn is a useful skeleton). Its lost functions are exactly what the "
                         "confirmed rules above must supply; do not trust values in it.\n" + self.lead_code)
        parts.append(f"TASK: write the complete {self.language.engine_file}. Reply with one "
                     f"```{self.language.fence} block and nothing else. {self.language.prompt_hints}")
        return "\n\n".join(parts)

    def repair_prompt(self, engine_js: str, counterexample: dict[str, Any],
                      rules: list[dict[str, Any]]) -> str:
        ce = counterexample
        trace = "\n".join(
            f"  step {t['step']}: action={json.dumps(t['action'])}\n    events={json.dumps(t['events'])}\n"
            f"    view={json.dumps(t['view'])}" for t in ce.get("reference_trace", []))
        return "\n\n".join([
            self.language.engine_api,
            f"Your {self.language.engine_file} below is BLOCKED by the Verifier. The original program behaves differently "
            "on the shortest game that exposes it.",
            f"```{self.language.fence}\n{engine_js}\n```",
            f"COUNTEREXAMPLE ({ce.get('source')}): players={ce.get('players')}, "
            f"deck={json.dumps((ce.get('script') or {}).get('deck'))}\nactions played: "
            f"{json.dumps(ce.get('actions'))}\nWhat the ORIGINAL did (ground truth):\n{trace}\n"
            f"Divergence kind: {ce.get('kind')}\nOriginal reply: {json.dumps(ce.get('reference'))[:1500]}\n"
            f"Your rebuild replied: {json.dumps(ce.get('candidate'))[:1500]}"
            + (f"\nYour rebuild's error message: {ce['candidate_error']}" if ce.get("candidate_error") else ""),
            "For reference, the confirmed rules (Python):\n" + "\n\n".join(
                f"[{r['group']}] {r['text']}\n{r['code']}" for r in rules),
            f"Fix {self.language.engine_file} so it matches the original on this game without breaking the "
            f"rules above. Reply with the complete corrected {self.language.engine_file} in one "
            f"```{self.language.fence} block and nothing else."])

    # ---------- build / repair ----------
    def _finish(self, directory: Path, engine_js: str) -> RebuildResult:
        if self.language is JAVASCRIPT:
            write_rebuild(directory, engine_js)
        else:
            self.language.write(directory, engine_js)
        result = RebuildResult(directory, engine_js, language=self.language)
        error = node_check(directory / "engine.js") if self.language is JAVASCRIPT \
            else self.language.check(directory)
        result.syntax_ok = error is None
        if error:
            result.problems.append(f"syntax: {error}")
            return result
        result.unknown_behaviors = (unknown_behaviors_of(directory) if self.language is JAVASCRIPT
                                    else self.language.unknown(directory))
        games = self.store.fitting().games()
        script = next((g for g in games if g.players == 2), games[0]).script["deck"]
        problem = smoke_game(result.command, script)
        result.smoke_ok = problem is None
        if problem:
            result.problems.append(f"smoke: {problem}")
        return result

    def build(self, rules: list[dict[str, Any]], directory: Path) -> RebuildResult:
        if not rules:
            raise RebuildError("no Confirmed rules: nothing to build from")
        text = self.muse.complete(self.build_prompt(rules), system=SYSTEM).text
        return self._finish(directory, self._extract(text))

    def repair(self, current: RebuildResult, counterexample: dict[str, Any],
               rules: list[dict[str, Any]]) -> RebuildResult:
        text = self.muse.complete(self.repair_prompt(current.engine_js, counterexample, rules),
                                  system=SYSTEM).text
        return self._finish(current.directory, self._extract(text))


@dataclass
class LoopResult:
    accepted: bool
    attempts: int
    result: RebuildResult | None
    history: list[dict[str, Any]] = field(default_factory=list)


def build_and_repair(rebuilder: Rebuilder, rules: list[dict[str, Any]], directory: Path,
                     verify: Callable[[list[str]], Any], *, max_attempts: int = 4,
                     on_counterexample: Callable[[dict[str, Any]], None] | None = None,
                     events: EventLog | None = None, language: str = "JavaScript") -> LoopResult:
    """Build, verify, and repair from counterexamples until Accepted or out of attempts."""
    history: list[dict[str, Any]] = []
    result = rebuilder.build(rules, directory)
    safe_emit(events, "rebuild", {"language": language, "runnable": result.ready,
                                 "unknown": len(result.unknown_behaviors)})
    for attempt in range(1, max_attempts + 1):
        entry: dict[str, Any] = {"attempt": attempt, "problems": result.problems,
                                 "unknown_behaviors": result.unknown_behaviors}
        history.append(entry)
        if not result.ready:
            entry["verdict"] = "not runnable"
            ce: dict[str, Any] = {"source": "build", "kind": "build", "actions": [],
                                  "reference": None, "candidate": result.problems}
        else:
            verdict = verify(result.command)
            safe_emit(events, "verify", {
                "verdict": verdict.verdict, "reason": str(verdict.reason).split(":")[0][:120],
                "attempt": attempt,
                "games": getattr(getattr(verdict, "fuzz", None), "games", 0),
                "divergences": len(getattr(getattr(verdict, "replay", None), "divergences", []) or [])})
            entry["verdict"] = verdict.verdict
            entry["reason"] = verdict.reason
            if verdict.verdict == "Accepted":
                return LoopResult(True, attempt, result, history)
            ce = verdict.counterexample or {"source": "gate", "kind": "gate", "actions": [],
                                            "reference": None, "candidate": verdict.reason}
            if on_counterexample:
                on_counterexample(ce)
        if attempt == max_attempts:
            break
        safe_emit(events, "repair", {"attempt": attempt + 1, "kind": ce.get("kind")})
        result = rebuilder.repair(result, ce, rules)
        safe_emit(events, "rebuild", {"language": language, "runnable": result.ready,
                                     "unknown": len(result.unknown_behaviors)})
    return LoopResult(False, max_attempts, result, history)
