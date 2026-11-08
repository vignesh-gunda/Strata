"""T15.5: what may never appear in a feed line or an event.

Three things are kept out: anything the prompt firewall blocks (`services/`, sealed
files), API keys, and raw model output (code, JSON, long free text). Callers get a yes
or no; the offending text is never echoed back.
"""
from __future__ import annotations

import os
import re
from typing import Any

from llm.main import firewall

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
_SECRET_SHAPES = re.compile(
    r"\bsk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S{12,}|\b[A-Fa-f0-9]{40,}\b|\b[A-Za-z0-9+/_=-]{48,}")
# code and data pasted straight from a model: fences, JSON objects, source keywords
_RAW_MODEL = re.compile(r"```|^\s*(\{|\[\s*[\[{\"])|[{}]\s*$|\bdef\s+\w+\s*\(|\bimport\s+\w+|\breturn\b.+\bif\b|=>|\bfunction\s*\(",
                        re.I | re.M)
_MAX_QUOTE_WORDS = 14


def _secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= 8]


def _flatten(part: Any) -> str:
    if isinstance(part, str):
        return part
    try:
        import json
        return json.dumps(part, default=str)
    except (TypeError, ValueError):
        return str(part)


def has_secret(*parts: Any) -> bool:
    text = " ".join(_flatten(p) for p in parts)
    return any(value in text for value in _secret_values()) or bool(_SECRET_SHAPES.search(text))


def looks_like_raw_model_output(text: str) -> bool:
    """Code, JSON or a wall of prose. Short claim text is fine to quote; this is not."""
    return bool(_RAW_MODEL.search(text)) or len(text.split()) > _MAX_QUOTE_WORDS * 4


def is_safe(*parts: Any) -> bool:
    """True when nothing here is restricted, secret or raw model output."""
    try:
        firewall.check(*parts)
    except firewall.PromptFirewallError:
        return False
    if has_secret(*parts):
        return False
    return not any(isinstance(p, str) and looks_like_raw_model_output(p) for p in _strings(parts))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
