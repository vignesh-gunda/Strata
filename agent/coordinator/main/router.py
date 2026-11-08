"""T10.2: turn one chat line into one intent.

Obvious lines are routed by keyword rules (no model call). Anything unclear goes to
SubQ `choice()`. If SubQ is down or says it is unsure, we fall back to the keyword
guess, then to `unknown`. A router mistake can only pick the wrong *view* of the
ledger; it can never change what is true.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from llm.main.firewall import PromptFirewallError
from llm.main.subq import SubqError

from .reliability import with_retry


class Intent(str, Enum):
    START_RUN = "start_run"
    STATUS = "status"
    LEDGER = "ledger"
    OPEN_QUESTIONS = "open_questions"
    EXPLAIN_CLAIM = "explain_claim"
    SHOW_REBUILD = "show_rebuild"
    SHOW_REPORT = "show_report"
    HELP = "help"
    UNKNOWN = "unknown"


OPTIONS: dict[str, str] = {
    Intent.START_RUN.value: "The user wants to start (or resume) the reverse-engineering run.",
    Intent.STATUS.value: "The user asks how far along the current run is.",
    Intent.LEDGER.value: "The user wants the list or summary of what has been found: rules, claims, findings, counts.",
    Intent.OPEN_QUESTIONS.value: "The user asks what is still unknown, unproven or open.",
    Intent.EXPLAIN_CLAIM.value: "The user asks about one specific rule or claim: why it is believed, what evidence backs it.",
    Intent.SHOW_REBUILD.value: "The user wants to see the rebuilt game: its state, files or whether it was accepted.",
    Intent.SHOW_REPORT.value: "The user wants the final report or a summary of results.",
    Intent.HELP.value: "The user asks what this agent can do or how to use it.",
    Intent.UNKNOWN.value: "None of the above.",
}

INSTRUCTIONS = (
    "Route one chat message sent to a local agent that reverse-engineers a game. "
    "Pick the single best matching intent. Pick unknown if nothing fits."
)

MIN_CONFIDENCE = 0.5

_RULES: list[tuple[Intent, re.Pattern[str]]] = [
    (Intent.EXPLAIN_CLAIM, re.compile(r"\bc-\d{3,}\b|\b(explain|why|evidence|prove[sd]?|proof)\b", re.I)),
    (Intent.OPEN_QUESTIONS, re.compile(r"\b(open (questions?|items?)|still (unknown|open)|unknowns?|unproven|not (yet )?(known|proven))\b", re.I)),
    (Intent.SHOW_REBUILD, re.compile(r"\b(rebuild|rebuilt|engine\.js|the (new )?implementation)\b", re.I)),
    (Intent.SHOW_REPORT, re.compile(r"\breport\b", re.I)),
    (Intent.STATUS, re.compile(r"\b(status|progress|how far|where are we|running)\b", re.I)),
    (Intent.LEDGER, re.compile(r"\b(ledger|claims?|findings?|confirmed|refuted|rules found)\b", re.I)),
    (Intent.START_RUN, re.compile(r"^\s*(please\s+)?(start|begin|run|resume|kick off|go)\b", re.I)),
    (Intent.HELP, re.compile(r"^\s*(help|\?|what can you do|commands)\b", re.I)),
]
# Lines where a plain keyword hit is ambiguous, so SubQ gets the final say:
# "rebuild it in Go" is a start request, not a request to show the rebuild.
_START_VERBS = re.compile(r"\b(start|begin|kick off|resume|rebuild it|rebuild the|run the)\b", re.I)


class ChoiceClient(Protocol):
    def choice(self, state: Any, instructions: str, options: dict[str, str | None]) -> Any: ...


@dataclass(frozen=True)
class Route:
    intent: Intent
    via: str                    # "rules" | "subq" | "fallback"
    confidence: float = 1.0
    note: str = ""


def keyword_intent(text: str) -> Intent | None:
    for intent, pattern in _RULES:
        if pattern.search(text):
            if intent is Intent.SHOW_REBUILD and _START_VERBS.search(text):
                return Intent.START_RUN
            return intent
    return None


def _is_obvious(text: str, intent: Intent | None) -> bool:
    """Short, single-keyword lines are safe to route without a model."""
    if intent is None:
        return False
    if intent is Intent.EXPLAIN_CLAIM and not re.search(r"\bc-\d{3,}\b", text):
        return False            # "explain the rules" needs the model to tell what is asked
    return len(text.split()) <= 4


class Router:
    def __init__(self, subq: ChoiceClient | None, *, sleep: Callable[[float], None] | None = None):
        self._subq = subq
        self._sleep = sleep

    def obvious(self, text: str) -> Route | None:
        """A short, unambiguous command routed by keywords alone (no model)."""
        guess = keyword_intent(text)
        return Route(guess, "rules") if _is_obvious(text, guess) else None

    def route(self, text: str) -> Route:
        guess = keyword_intent(text)
        if _is_obvious(text, guess):
            return Route(guess, "rules")
        if self._subq is None:
            return self._fallback(guess, "no model configured")
        try:
            result = with_retry(
                lambda: self._subq.choice({"message": text}, INSTRUCTIONS, dict(OPTIONS)),
                attempts=3, sleep=self._sleep, retry_on=_is_transient)
        except PromptFirewallError:
            raise
        except Exception as exc:    # SubQ down, out of credits, bad request...
            return self._fallback(guess, f"router model unavailable ({type(exc).__name__})")
        try:
            picked = Intent(result.choice)
        except ValueError:
            return self._fallback(guess, "router model returned an unknown choice")
        if result.confidence < MIN_CONFIDENCE and guess is not None and guess is not picked:
            return Route(guess, "fallback", result.confidence, "model unsure, used keyword match")
        return Route(picked, "subq", float(result.confidence))

    @staticmethod
    def _fallback(guess: Intent | None, why: str) -> Route:
        if guess is not None:
            return Route(guess, "fallback", 0.5, why)
        return Route(Intent.UNKNOWN, "fallback", 0.0, why)


def _is_transient(exc: BaseException) -> bool:
    """Retry network-ish failures; never retry 400/402/422 or a firewall block."""
    if isinstance(exc, PromptFirewallError):
        return False
    if isinstance(exc, SubqError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, (OSError, TimeoutError, ConnectionError))
