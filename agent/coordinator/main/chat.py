"""The local chat loop. Terminal today; a UI app can call `Coordinator.handle` the same way.

    python -m coordinator.main.chat --run-id r-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from concierge.main import scope
from concierge.main.concierge import Concierge
from llm.main import firewall
from llm.main.subq import SubqClient
from rebuilder.main.languages import language_in

from . import answers
from .reliability import load_ledger
from .router import ChoiceClient, Intent, Router
from .runs import AGENT_ROOT, Emit, RunManager, StageRunner, subprocess_stage

HELP = """I reverse-engineer the Pressure game and rebuild it. You can ask me to:
  start               run the whole thing (survey, probe, test rules, rebuild); safe to repeat, it resumes
  rebuild it in Go    the same, rebuilding in the language you name (JavaScript or Go)
  any question        e.g. "what did you find wrong in the manual?" gets a short plain answer
  status              how far the run is
  ledger              what has been found, in counts
  open questions      what is still unknown
  explain <rule or c-0001>   why a rule is believed, with trace IDs
  show rebuild        the rebuilt game's state
  show report         results so far"""


class Coordinator:
    def __init__(self, runs_dir: Path, run_id: str, emit: Emit, *,
                 subq: ChoiceClient | None = None, muse=None, stage_runner: StageRunner = subprocess_stage,
                 sleep: Callable[[float], None] | None = None, which=None):
        self.run_id = run_id
        self.run_dir = Path(runs_dir) / run_id
        self.router = Router(subq, sleep=sleep)
        self.runner = RunManager(runs_dir, run_id, emit, stage_runner=stage_runner, sleep=sleep)
        # With Muse available, free-form lines go to the concierge (T14); without it the T10 router
        # (keywords, then SubQ) handles them, so the agent still works with one key missing.
        self.muse = muse
        self.concierge = Concierge(self.run_dir, self.runner.start, self.runner.status_line,
                                   muse=muse, subq=subq, which=which)

    def handle(self, text: str) -> str:
        """Reply to one chat line right away. Long work continues in the background."""
        text = text.strip()
        if not text:
            return HELP
        allowed = scope.check_question(text)
        if not allowed.allowed:
            return allowed.reply
        try:
            route = self.router.obvious(text)
            if route is None:
                if self.muse is not None:
                    return self.concierge.handle(text)
                route = self.router.route(text)
        except firewall.PromptFirewallError:
            return scope.REFUSED
        reply = self._dispatch(route.intent, text)
        try:
            firewall.check(reply)
        except firewall.PromptFirewallError:
            return "I can't show that answer because it touches restricted material."
        return reply

    def _dispatch(self, intent: Intent, text: str) -> str:
        if intent is Intent.START_RUN:
            return self.concierge.rebuild(language_in(text))
        if intent is Intent.STATUS:
            return self.runner.status_line()
        if intent is Intent.HELP:
            return HELP
        if intent is Intent.SHOW_REBUILD:
            return answers.rebuild_summary(self.run_dir)
        if intent is Intent.UNKNOWN:
            return "I can only help with this reverse-engineering run.\n" + HELP

        view = load_ledger(self.run_dir)
        if view.ledger is None:
            return answers.NO_RUN if not view.degraded else f"I can't read the ledger: {view.note}."
        if intent is Intent.SHOW_REPORT:
            body = answers.report_summary(self.run_dir, view.ledger)
        elif intent is Intent.OPEN_QUESTIONS:
            body = answers.open_questions(view.ledger)
        elif intent is Intent.EXPLAIN_CLAIM:
            body = answers.explain_claim(view.ledger, text)
        else:
            body = answers.ledger_summary(view.ledger)
        return f"({view.note})\n{body}" if view.degraded else body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local chat for the Strata agent.")
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--no-model", action="store_true", help="route with keywords only")
    args = parser.parse_args(argv)

    subq = muse = None
    if not args.no_model:
        try:
            subq = SubqClient()
        except RuntimeError as exc:
            print(f"(no SubQ router model: {exc})")
        try:
            from llm.main.muse import MuseClient
            muse = MuseClient()
        except RuntimeError as exc:
            print(f"(no Muse: {exc}; free-form questions fall back to keywords)")

    def emit(message: str) -> None:
        print(f"\n  > {message}\nyou: ", end="", flush=True)

    coordinator = Coordinator(Path(args.runs_dir), args.run_id, emit, subq=subq, muse=muse)
    print("Strata agent. Type 'help'. Ctrl-D to quit.")
    while True:
        try:
            line = input("you: ")
        except EOFError:
            print()
            return 0
        print(coordinator.handle(line))


if __name__ == "__main__":
    sys.exit(main())
