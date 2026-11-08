"""A claim as extracted from an artifact, before it enters the ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClaimSpec:
    key: str                              # stable within one artifact
    text: str
    topic: str | None = None
    rule: dict[str, Any] | None = None    # machine-checkable form, has a "type"
    kind: str = "behavior"                # behavior | unknown
    subkind: str | None = None            # missing_function, parameter, hidden_state, ...
    notes: list[str] = field(default_factory=list)
