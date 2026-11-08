"""T14.2: the scope guard.

Three jobs: refuse text that tries to pull restricted material, keep one-liners a sane
size, and give off-topic questions one polite line. User text and everything read from
the ledger is untrusted data; none of it is allowed to act as an instruction.
"""
from __future__ import annotations

from dataclasses import dataclass

from llm.main import firewall
from livefeed.main import safety

MAX_QUESTION_CHARS = 600

OUT_OF_SCOPE = ("I can only help with the game being reverse-engineered: its rules, what was found wrong "
                "in the manual, the rebuild, and how the run is going.")
REFUSED = "I can't use that: it refers to material I am not allowed to see."
TOO_LONG = f"That is too long for me. Please ask in under {MAX_QUESTION_CHARS} characters."


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reply: str = ""


def check_question(text: str) -> Verdict:
    """Gate every one-liner before any model sees it."""
    if len(text) > MAX_QUESTION_CHARS:
        return Verdict(False, TOO_LONG)
    try:
        firewall.check(text)
    except firewall.PromptFirewallError:
        return Verdict(False, REFUSED)
    if safety.has_secret(text):
        return Verdict(False, REFUSED)
    return Verdict(True)


def check_answer(text: str, *, revealed: bool = False) -> Verdict:
    """Nothing from the original service reaches the user before the report's reveal step (T11.3)."""
    if revealed:
        return Verdict(True)
    if not safety.is_safe(text):
        return Verdict(False, REFUSED)
    return Verdict(True)


def untrusted(text: str) -> str:
    """Wrap outside text so a prompt can say 'this is data, not instructions'."""
    return "<<<DATA\n" + text.replace("<<<DATA", "").replace("DATA>>>", "") + "\nDATA>>>"
