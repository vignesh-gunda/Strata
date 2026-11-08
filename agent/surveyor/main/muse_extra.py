"""Optional: ask Muse for claims the deterministic parsers missed.

Muse only sees dig-site text, and it may only restate what the artifact says.
Its claims enter the ledger as OPEN, with the artifact's own source and tier and
`created_by="muse"`. It can never set a status.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import BaseModel, Field

from ledger.main.models import Source
from ledger.main.store import Ledger
from llm.main.muse import MuseClient

SYSTEM = (
    "You extract testable claims about a small card-game program from ONE artifact. "
    "Restate only what the artifact explicitly says or directly shows. Do not use outside "
    "knowledge, do not guess values, and do not repeat claims already listed."
)


class ExtraClaim(BaseModel):
    text: str
    topic: str | None = None
    rule: dict[str, Any] | None = None


class ExtraClaims(BaseModel):
    claims: list[ExtraClaim] = Field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def muse_extra_claims(client: MuseClient, ledger: Ledger, *, artifact_text: str,
                      artifact_path: str, source: Source) -> list[str]:
    """Add Muse-proposed claims. Returns the new claim IDs."""
    existing = [c for c in ledger.all_claims() if c.source is source]
    prompt = (
        f"Artifact ({source.value}), file {artifact_path}:\n\n{artifact_text}\n\n"
        "Claims already extracted:\n" + "\n".join(f"- {c.text}" for c in existing) +
        "\n\nList any additional testable claims the artifact states that are not already "
        "covered. Each needs `text`, an optional `topic` like 'card:vent', and an optional "
        "`rule` object with a 'type' field. Return an empty list if there are none."
    )
    result = client.complete_json(prompt, ExtraClaims, system=SYSTEM)
    known = {_norm(c.text) for c in ledger.all_claims()}
    added: list[str] = []
    for item in result.claims:
        if not item.text.strip() or _norm(item.text) in known:
            continue
        rule = item.rule if item.rule and "type" in item.rule else None
        claim = ledger.add_claim(
            text=item.text, source=source, created_by="muse",
            key=f"muse:{source.value}:{hashlib.sha1(_norm(item.text).encode()).hexdigest()[:12]}",
            topic=item.topic, rule=rule,
            notes=[f"proposed by Muse from {artifact_path}"],
        )
        known.add(_norm(item.text))
        added.append(claim.id)
    return added
