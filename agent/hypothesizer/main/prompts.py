"""Build the prompt that asks Muse for competing rules in one group."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from auditor.main.corpus import FittingCorpus
from auditor.main.digest import game_digest, step_line
from ledger.main.models import Claim, Status

from .groups import CONTRACT, GROUPS, Group

SYSTEM = (
    "You reverse-engineer a small card-game program from recorded runs. You are a careful "
    "scientist: you propose several COMPETING explanations as executable rules, and a "
    "deterministic scorer decides which survive. Documentation and recovered code are leads "
    "that may be wrong; recorded traces are the only truth."
)

SETUP_STEP_KEYS = ("trace_id", "seat", "turn", "action", "legal", "events", "before", "view")


def leads_block(claims: list[Claim], findings_text: list[str]) -> str:
    rows = []
    for c in claims:
        if (c.rule or {}).get("type") == "executable":
            continue
        rows.append(f"- [{c.source.value}/{c.tier.value}/{c.status.value}] {c.text}")
    block = "\n".join(rows[:140])
    if findings_text:
        block += "\n\nKnown disagreements between artifacts and traces:\n" + "\n".join(
            f"- {t}" for t in findings_text)
    return block


def focus_steps(group: Group, corpus: FittingCorpus, per_signature: int = 2,
                limit: int = 14) -> list[dict[str, Any]]:
    """Representative steps for a group: one or two per distinct pattern of events."""
    seen: dict[tuple, int] = defaultdict(int)
    chosen = []
    for _, step in corpus.steps():
        if group.kind == "events":
            mine = tuple(e["type"] for e in step["events"] if e["type"] in group.covers)
            signature = (step["action"].get("card") or step["action"]["type"], mine)
            if not mine and seen[signature] >= 1:
                continue
        else:
            signature = (step["action"].get("card") or step["action"]["type"],
                         tuple(e["type"] for e in step["events"]))
        if seen[signature] < per_signature:
            seen[signature] += 1
            chosen.append(step)
    # keep the rarer patterns: they are the informative ones
    chosen.sort(key=lambda s: -sum(1 for e in s["events"] if e["type"] in
                                   ("eliminated", "revealed", "game_over", "passed")))
    return chosen[:limit]


def build_prompt(group_name: str, *, corpus: FittingCorpus, claims: list[Claim],
                 findings_text: list[str], lead_code: str, accepted: list[Claim],
                 feedback: list[dict[str, Any]], count: int = 3,
                 games_in_digest: int = 6) -> str:
    group = GROUPS[group_name]
    games = corpus.games()
    # a spread of games: experiments (probe) first because they are small and targeted
    probes = [g for g in games if g.origin == "probe"][:3]
    corpus_games = [g for g in games if g.origin == "corpus"]
    stride = max(1, len(corpus_games) // max(1, games_in_digest - len(probes)))
    digest_games = probes + corpus_games[::stride][:games_in_digest - len(probes)]

    parts = [CONTRACT,
             f"TARGET GROUP: {group.name}  (KIND = \"{group.kind}\""
             + (f", COVERS = {list(group.covers)}" if group.covers else "") + ")\n" + group.doc]
    parts.append("LEADS (documentation and recovered code; may be wrong, tiers shown):\n"
                 + leads_block(claims, findings_text))
    if lead_code:
        parts.append("RECOVERED SOURCE FRAGMENT (some bodies lost, imports missing; "
                     "structure only, never trust values):\n" + lead_code)
    if accepted:
        parts.append("RULES ALREADY ACCEPTED FOR OTHER GROUPS (for context):\n" + "\n".join(
            f"- [{c.rule['group']}] {c.text}" for c in accepted))
    examples = focus_steps(group, corpus)
    parts.append("EXAMPLE STEPS, full detail (the 'before' view is the state before the action):\n"
                 + "\n".join(json.dumps({k: s[k] for k in SETUP_STEP_KEYS if k in s})
                             for s in examples[:4]))
    parts.append("MORE STEPS, one line each:\n" + "\n".join(step_line(s) for s in examples[4:]))
    parts.append("WHOLE GAMES (compact):\n" + "\n\n".join(game_digest(g, max_steps=45)
                                                          for g in digest_games))
    if group.kind == "setup":
        parts.append("DECKS AND OPENING VIEWS:\n" + "\n".join(
            f"players={g.players} deck[:20]={g.script['deck'][:20]} len={len(g.script['deck'])} "
            f"initial_view={json.dumps(g.initial_view)}" for g in games[:4]))
    if feedback:
        parts.append("FEEDBACK FROM THE SCORER ON YOUR PREVIOUS ATTEMPTS (fix these):\n"
                     + json.dumps(feedback, indent=1)[:12000])
    parts.append(
        f"TASK: write {count} COMPETING rules for group '{group.name}' that explain every "
        "recorded step. Make them genuinely different hypotheses (for example differing in how "
        "a hidden state works), not copies. Each rule must define KIND"
        + (f", COVERS = {list(group.covers)}" if group.covers else "")
        + " and predict(). Return JSON: {\"rules\": [{\"name\": str, \"description\": str, "
        "\"code\": str}]} where code is the full module text.")
    return "\n\n".join(parts)
