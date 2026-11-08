"""SubQ Snap client (long-context decisions: noul, choice, score)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import firewall
from .budget import Budget
from .env import load_env, require_key
from .http import Transport, post_with_retry, urllib_transport

DEFAULT_BASE = "https://api.subq.ai/v1"
MODEL = "subq-snap-1.1"
_MEANINGS = {
    400: "input over the 384K-token limit or rejected request",
    401: "missing or invalid API key",
    402: "insufficient credits",
    403: "key cannot use this model",
    422: "invalid request",
}


class SubqError(RuntimeError):
    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(f"SubQ HTTP {status} {error_type}: {message}")
        self.status = status
        self.error_type = error_type


@dataclass
class Choice:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass
class Score:
    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


def noul_question(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        question["criteria"] = {"true": true, "false": false}
    return question


def choice_question(instructions: str, options: dict[str, str | None]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score_question(instructions: str, levels: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


class SubqClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        budget: Budget | None = None,
        base_url: str | None = None,
        model: str = MODEL,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if api_key is None:
            load_env()
            api_key = require_key("SUBQ_API_KEY")
        self._key = api_key
        self.budget = budget or Budget()
        self.base_url = base_url or os.environ.get("SUBQ_API_BASE", DEFAULT_BASE)
        self.model = model
        self._transport = transport
        self._sleep = sleep

    def decide(self, state: Any, questions: dict[str, dict]) -> dict[str, dict]:
        """Send one state and 1-128 questions. Returns the `answers` map."""
        firewall.check(state, questions)
        self.budget.ensure("subq")
        status, payload = post_with_retry(
            f"{self.base_url}/systemone", self._key,
            {"model": self.model, "state": state, "questions": questions},
            transport=self._transport, sleep=self._sleep,
        )
        if status != 200:
            detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
            raise SubqError(
                status,
                detail.get("error_type", "error"),
                detail.get("message") or _MEANINGS.get(status, "request failed"),
            )
        self.budget.record("subq", payload.get("usage", {}).get("input_tokens", 0))
        return payload["answers"]

    def noul(self, state: Any, instructions: str, **criteria: str) -> float:
        """Probability (0-1) of yes."""
        answers = self.decide(state, {"q": noul_question(instructions, **criteria)})
        return float(answers["q"]["noul"])

    def choice(self, state: Any, instructions: str, options: dict[str, str | None]) -> Choice:
        a = self.decide(state, {"q": choice_question(instructions, options)})["q"]
        return Choice(a["choice"], a["probabilities"], a["confidence"])

    def score(self, state: Any, instructions: str, levels: list[str]) -> Score:
        a = self.decide(state, {"q": score_question(instructions, levels)})["q"]
        return Score(a["score"], a["legend"], a["probabilities"], a["confidence"])
