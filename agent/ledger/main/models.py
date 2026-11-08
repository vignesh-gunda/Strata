"""Ledger data models: claims, findings, predictions."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Source(str, Enum):
    """Where a claim came from. Never changes."""
    MANUAL = "manual"
    CODE = "code"
    BINARY = "binary"
    TRACE = "trace"
    MODEL = "model"


class Tier(str, Enum):
    """How much the claim is trusted. Upgrades to OBSERVED only when confirmed."""
    DOCUMENTED = "documented"
    READ_FROM_CODE = "read_from_code"
    OBSERVED = "observed"
    INFERRED = "inferred"


class Status(str, Enum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    OPEN = "open"


# The tier a claim starts with, by source.
TIER_FOR_SOURCE = {
    Source.MANUAL: Tier.DOCUMENTED,
    Source.CODE: Tier.READ_FROM_CODE,
    Source.BINARY: Tier.OBSERVED,
    Source.TRACE: Tier.OBSERVED,
    Source.MODEL: Tier.INFERRED,
}


class Claim(BaseModel):
    id: str
    key: str | None = None            # stable identity so re-running a step adds nothing twice
    text: str
    kind: str = "behavior"            # behavior | unknown
    subkind: str | None = None        # missing_function, parameter, hidden_state, ...
    topic: str | None = None          # grouping, e.g. "card:vent"
    rule: dict[str, Any] | None = None  # machine-checkable form, always has a "type"
    source: Source
    tier: Tier
    status: Status = Status.OPEN
    evidence: list[str] = Field(default_factory=list)       # trace IDs
    experiment_ids: list[str] = Field(default_factory=list)
    retested_in: list[str] = Field(default_factory=list)    # trace IDs from new situations
    notes: list[str] = Field(default_factory=list)
    created_by: str
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class FindingKind(str, Enum):
    STALE = "stale"            # artifact was once right, traces show it changed
    WRONG = "wrong"            # artifact contradicts traces
    INCOMPLETE = "incomplete"  # artifact omits something traces show
    MISSING = "missing"        # artifact lost the information (e.g. lost function body)


class Finding(BaseModel):
    id: str
    claim_id: str
    artifact: str              # "manual", "code", ...
    artifact_said: str
    traces_said: str
    trace_ids: list[str] = Field(default_factory=list)
    kind: FindingKind
    created_at: str = Field(default_factory=now)


class Prediction(BaseModel):
    id: str
    seq: int
    game_id: str
    step: int
    prediction: dict[str, Any]
    claim_ids: list[str] = Field(default_factory=list)
    logged_at: str = Field(default_factory=now)


class Outcome(BaseModel):
    prediction_id: str
    actual: dict[str, Any]
    surprise: bool
    trace_id: str | None = None
    recorded_at: str = Field(default_factory=now)
