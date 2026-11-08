"""Muse designs separating experiments; SubQ picks which one to run next."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from auditor.main.digest import step_line
from ledger.main.store import Ledger
from llm.main.muse import MuseClient

from .experiments import Experiment, validate_experiment

SYSTEM = (
    "You design small experiments for a card-game program you can only run, not read. "
    "An experiment fixes each seat's starting hand and the draw pile, then lists the actions "
    "to play. Its purpose is to make competing hypotheses predict DIFFERENT outcomes."
)


class Designs(BaseModel):
    experiments: list[Experiment] = Field(default_factory=list)


def design_experiments(muse: MuseClient, *, hand_size: int, known_cards: set[str],
                       hypotheses: list[dict[str, Any]], sample_steps: list[dict[str, Any]],
                       existing_ids: set[str], count: int = 3) -> tuple[list[Experiment], list[str]]:
    """Return (valid experiments, rejection messages). `hypotheses` are competing rules to separate."""
    prompt = (
        f"Game facts: hands are dealt round-robin, {hand_size} cards per seat; the rest is the draw "
        f"pile, top first. Card names: {sorted(known_cards)}. Seats play in turn; each action is "
        '{"type":"play","card":NAME}, {"type":"draw"} or {"type":"pass"}. If an action is illegal '
        "the runner substitutes a safe one and records a deviation, so keep sequences legal.\n\n"
        "Competing hypotheses (code is a predictor of the next step):\n"
        + "\n\n".join(f"[{h['id']}] {h['text']}\n{h['code']}" for h in hypotheses)
        + "\n\nExample recorded steps:\n" + "\n".join(step_line(s) for s in sample_steps[:12])
        + f"\n\nDesign up to {count} experiments (ids must not be in {sorted(existing_ids)}; "
        "use ids like e-m1). Each needs: id, title, players (2-4), hands (one list of exactly "
        f"{hand_size} cards per seat), pile, actions (a list of play/draw/pass) or a policy, "
        "claim_topics, rationale. Put cards in the hands so the hypotheses disagree on what happens."
    )
    designs = muse.complete_json(prompt, Designs, system=SYSTEM)
    valid, rejected = [], []
    taken = set(existing_ids)
    for exp in designs.experiments[:count]:
        errors = validate_experiment(exp, hand_size, known_cards)
        if exp.id in taken:
            errors.append(f"id {exp.id} already used")
        if errors:
            rejected.append(f"{exp.id}: {'; '.join(errors)}")
        else:
            valid.append(exp)
            taken.add(exp.id)
    return valid, rejected


def select_experiment(subq: Any, ledger: Ledger, candidates: list[Experiment],
                      probe_list: list[dict[str, Any]]) -> Experiment:
    """Ask SubQ which experiment is most informative; fall back to the probe-list order."""
    if len(candidates) == 1:
        return candidates[0]
    ranks = {item["topic"]: item["rank"] for item in probe_list if item.get("topic")}

    def fallback_key(exp: Experiment) -> tuple[int, str]:
        best = min((ranks.get(t, 10_000) for t in exp.claim_topics), default=10_000)
        return best, exp.id

    if subq is not None:
        from llm.main.subq import SubqError
        state = {
            "ledger": ledger.counts(),
            "open_questions": [{"topic": p["topic"], "text": p["text"], "rank": p["rank"]}
                               for p in probe_list[:25]],
            "surprises": [{"game": p.game_id, "step": p.step, "claims": p.claim_ids}
                          for p, _ in ledger.surprises()][-10:],
            "candidates": {e.id: {"title": e.title, "topics": e.claim_topics,
                                  "rationale": e.rationale} for e in candidates},
        }
        options = {e.id: f"{e.title}: {e.rationale}" for e in candidates}
        try:
            answer = subq.choice(
                json.loads(json.dumps(state, default=str)),
                "Which experiment would teach us the most about the highest-ranked open questions "
                "and the recent surprises?", options)
            for exp in candidates:
                if exp.id == answer.choice:
                    return exp
        except SubqError:
            pass
    return min(candidates, key=fallback_key)
