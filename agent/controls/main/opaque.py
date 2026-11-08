"""T12.2: the opaque-token control.

Every card name is replaced by a meaningless token (`k07`) in the traces, the decks, the
action lists and the rules. If the Scorer's decisions depend only on behavior, every rule
ends with the same status as before. A rule that changes status was leaning on what a
name looks like (a `gear` prefix, say) rather than on what the card does.

`TokenMap` is also what an opaque live run would put between the agent and the binary.
"""
from __future__ import annotations

import copy
import random
import re
from typing import Any

from auditor.main.corpus import GameRecord, _add_before

from .replay import RuleSpec, rescore, rule_key


class TokenMap:
    def __init__(self, names: set[str] | list[str], seed: int = 7):
        ordered = sorted(set(names))
        tokens = [f"k{i:02d}" for i in range(len(ordered))]
        random.Random(seed).shuffle(tokens)                    # token order says nothing about name order
        self.forward = dict(zip(ordered, tokens))
        self.back = {t: n for n, t in self.forward.items()}

    def text(self, value: str) -> str:
        return self.forward.get(value, value)

    def untext(self, value: str) -> str:
        return self.back.get(value, value)

    def apply(self, node: Any, mapping: dict[str, str] | None = None) -> Any:
        """Relabel every string value and dict key that is exactly a card name."""
        m = self.forward if mapping is None else mapping
        if isinstance(node, str):
            return m.get(node, node)
        if isinstance(node, list):
            return [self.apply(x, m) for x in node]
        if isinstance(node, dict):
            return {m.get(k, k) if isinstance(k, str) else k: self.apply(v, m) for k, v in node.items()}
        return node

    def reveal(self, node: Any) -> Any:
        return self.apply(node, self.back)

    def rule_code(self, code: str) -> str:
        """Relabel quoted card-name literals in rule source code."""
        for name in sorted(self.forward, key=len, reverse=True):
            code = re.sub(rf"(['\"]){re.escape(name)}\1", lambda m, n=name: f"{m.group(1)}{self.forward[n]}{m.group(1)}", code)
        return code


def card_names(games: list[GameRecord]) -> set[str]:
    return {c for g in games for c in g.script.get("deck", [])}


def relabel_game(game: GameRecord, tokens: TokenMap) -> GameRecord:
    data = tokens.apply(copy.deepcopy(game.to_json()))
    data["game_id"] = game.game_id                            # ids are not card names
    for original, new in zip(game.steps, data["steps"]):
        new["trace_id"] = original["trace_id"]
    return GameRecord.from_json(data)


def relabel_rule(spec: RuleSpec, tokens: TokenMap) -> RuleSpec:
    code = tokens.rule_code(spec.code)
    return RuleSpec(rule_key(code), spec.group, spec.kind, spec.covers, code, tokens.text(spec.text))


def opaque_check(rules: list[RuleSpec], games: list[GameRecord], seed: int = 7) -> dict[str, Any]:
    """Score before and after relabeling and compare, rule by rule."""
    tokens = TokenMap(card_names(games), seed)
    plain = rescore(rules, games)
    masked_rules = [relabel_rule(r, tokens) for r in rules]
    masked = rescore(masked_rules, [relabel_game(g, tokens) for g in games])
    changed = []
    for original, masked_rule in zip(rules, masked_rules):
        before, after = plain.statuses[original.key], masked.statuses[masked_rule.key]
        if before != after:
            changed.append({"group": original.group, "text": original.text[:80], "before": before, "after": after})
    return {"tokens": len(tokens.forward), "rules": len(rules), "same_ledger": not changed,
            "changed": changed, "plain": plain.summary(), "opaque": masked.summary()}
