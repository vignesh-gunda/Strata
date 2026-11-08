"""T14.4 to T14.7 glued together: one line in, one reply out.

    rebuild  -> check the language and its toolchain, then start the background run
    explain  -> retrieve evidence from the ledger, write a short plain answer
    status   -> where the run is
    anything else -> one polite line
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from llm.main import firewall
from rebuilder.main.languages import (ToolchainMissing, UnsupportedLanguage, language_in, resolve)

from coordinator.main.reliability import load_ledger

from . import scope
from .retrieve import retrieve
from .understand import Understanding, understand
from .writer import write_answer


class Concierge:
    def __init__(self, run_dir: Path, start_run: Callable[..., str], status: Callable[[], str], *,
                 muse: Any = None, subq: Any = None,
                 which: Callable[[str], str | None] | None = None):
        self.run_dir = Path(run_dir)
        self._start, self._status = start_run, status
        self.muse, self.subq = muse, subq
        self._which = which

    def handle(self, text: str) -> str:
        verdict = scope.check_question(text)
        if not verdict.allowed:
            return verdict.reply
        try:
            result = understand(text, muse=self.muse, subq=self.subq)
        except firewall.PromptFirewallError:
            return scope.REFUSED
        what: Understanding = result.understanding
        if what.intent == "rebuild":
            reply = self.rebuild(what.language or language_in(text))
        elif what.intent == "status":
            reply = self._status()
        elif what.intent == "explain":
            reply = self.explain(what.question or text)
        else:
            reply = scope.OUT_OF_SCOPE
        checked = scope.check_answer(reply)
        return reply if checked.allowed else checked.reply

    # ---------- T14.3 / T14.4 ----------
    def rebuild(self, language_name: str | None) -> str:
        try:
            language = resolve(language_name)
            if self._which is None:
                language.require_toolchain()
            else:
                language.require_toolchain(self._which)
        except (UnsupportedLanguage, ToolchainMissing) as error:
            return str(error)
        return self._start(language)

    # ---------- T14.5 / T14.6 / T14.7 ----------
    def explain(self, question: str) -> str:
        view = load_ledger(self.run_dir, snapshot=False)
        if view.ledger is None:
            return "No run yet, so nothing is known. Ask me to rebuild the game and I will start finding out."
        evidence = retrieve(view.ledger, question, subq=self.subq)
        answer, _how = write_answer(question, evidence, self.muse)
        return answer if not view.degraded else f"({view.note})\n{answer}"
