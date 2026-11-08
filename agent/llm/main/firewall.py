"""Prompt firewall: nothing from the original service may reach a model.

Every prompt, system message and state payload passes through `check` before
it leaves the process. The agent must only know what `dig-site/` shows it.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Matches paths and file names that identify the original service or answer keys.
_FORBIDDEN = re.compile(
    r"services[/\\]|sealed[/\\]|\bsealed/runs\b|DIG_SITE_KEY|(?<![\w-])RULES\.md",
    re.IGNORECASE,
)


class PromptFirewallError(ValueError):
    """Raised when a prompt would leak the original service or answer keys."""


def check(*parts: Any) -> None:
    """Raise PromptFirewallError if any part mentions forbidden locations."""
    for part in parts:
        text = part if isinstance(part, str) else json.dumps(part, default=str)
        match = _FORBIDDEN.search(text)
        if match:
            raise PromptFirewallError(
                f"prompt blocked: contains forbidden reference {match.group(0)!r}"
            )
