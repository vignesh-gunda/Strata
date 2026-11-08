"""Meta Muse Spark client (open-ended work: hypotheses, experiments, code)."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import firewall
from .budget import Budget
from .env import load_env, require_key
from .http import Transport, post_with_retry, urllib_transport

T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE = "https://api.meta.ai/v1"
MODELS = ("muse-spark-1.3", "muse-spark-1.1")  # newest first, used as fallback
# How hard the model thinks before it answers. Lower is faster and cheaper. Muse Spark cannot turn
# reasoning off ("none" is an HTTP 400), so "minimal" is the fastest setting there is. Left unset,
# the model picks its own depth.
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def check_effort(effort: str | None) -> str | None:
    if effort in (None, ""):
        return None
    if effort == "none":
        raise ValueError("Muse Spark cannot turn reasoning off; use 'minimal' (fastest) or 'low'")
    if effort not in EFFORTS:
        raise ValueError(f"reasoning effort must be one of {', '.join(EFFORTS)}")
    return effort


class MuseError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Muse HTTP {status}: {message}")
        self.status = status


@dataclass
class MuseResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


def _extract_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in (None, "output_text"):
                    parts.append(part.get("text", ""))
    return "".join(parts)


def _extract_json(text: str) -> Any:
    """Parse JSON from model text, tolerating a fenced code block."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return json.loads((fenced.group(1) if fenced else text).strip())


class MuseClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        budget: Budget | None = None,
        base_url: str | None = None,
        models: tuple[str, ...] = MODELS,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
        effort: str | None = None,
    ):
        self.timeout = timeout          # seconds per HTTP attempt
        # default thinking depth for every call; STRATA_MUSE_EFFORT sets it without code changes
        self.effort = check_effort(effort if effort is not None else os.environ.get("STRATA_MUSE_EFFORT"))
        if api_key is None:
            load_env()
            api_key = require_key("META_API_KEY")
        self._key = api_key
        self.budget = budget or Budget()
        self.base_url = base_url or os.environ.get("META_API_BASE", DEFAULT_BASE)
        self.models = models
        self._transport = transport
        self._sleep = sleep
        self._model_index = 0  # sticks to a fallback once the first model 404s

    def complete(self, prompt: str, *, system: str | None = None, effort: str | None = None) -> MuseResult:
        firewall.check(prompt, system or "")
        level = check_effort(effort) or self.effort
        self.budget.ensure("muse")
        while True:
            model = self.models[self._model_index]
            body: dict[str, Any] = {"model": model, "input": prompt}
            if system:
                body["instructions"] = system
            if level:
                body["reasoning"] = {"effort": level}
            status, payload = post_with_retry(
                f"{self.base_url}/responses", self._key, body,
                transport=self._transport, sleep=self._sleep, timeout=self.timeout,
            )
            if status == 200:
                usage = payload.get("usage", {})
                result = MuseResult(
                    text=_extract_text(payload), model=model,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )
                self.budget.record("muse", result.total_tokens)
                return result
            if status == 404 and self._model_index + 1 < len(self.models):
                self._model_index += 1  # model_not_found: fall back
                continue
            error = payload.get("error", payload)
            message = error.get("message", "request failed") if isinstance(error, dict) else str(error)
            raise MuseError(status, message)

    def complete_json(self, prompt: str, schema: type[T], *, system: str | None = None,
                      effort: str | None = None) -> T:
        """Ask for JSON matching `schema`. Retries once, feeding back the error."""
        instruction = (
            f"{prompt}\n\nReturn only JSON matching this JSON schema, no prose:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        last_error = ""
        for attempt in range(2):
            text = self.complete(
                instruction if not last_error else
                f"{instruction}\n\nYour previous answer was invalid ({last_error}). Try again.",
                system=system, effort=effort,
            ).text
            try:
                return schema.model_validate(_extract_json(text))
            except (json.JSONDecodeError, ValidationError) as error:
                last_error = type(error).__name__
        raise MuseError(422, f"model did not return valid JSON for {schema.__name__}")
