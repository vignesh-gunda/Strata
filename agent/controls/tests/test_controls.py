"""T12 tests: the learning curve, the probe-order ablation and the opaque-token control, on toy games."""
from __future__ import annotations

import pytest

from auditor.main.corpus import GameRecord
from auditor.main.synthetic import make_game, make_view
from controls.main.ablation import (compare, coverage_curve, order_traces_only, order_with_artifacts,
                                    probes_to_reach, step_signature)
from controls.main.cli import main as controls_main
from controls.main.cli import markdown, run_controls
from controls.main.curve import baseline_is_low, learning_curve
from controls.main.opaque import TokenMap, card_names, opaque_check, relabel_game, relabel_rule
from controls.main.replay import RuleSpec, rescore, rule_key, rules_from_ledger
from ledger.main.models import Source, Status
from ledger.main.store import Ledger

PLAYED = ('KIND = "events"\nCOVERS = ["played"]\n'
          'def predict(history, view, action, prior_events):\n'
          '    if action.get("type") != "play":\n        return None\n'
          '    return [{"type": "played", "seat": view["current_seat"], "card": action["card"]}]\n')
GEAR_ONLY = PLAYED.replace('if action.get("type") != "play":', 'if action.get("type") != "play" or not action["card"].startswith("gear"):')
WRONG = PLAYED.replace('"card": action["card"]', '"card": "vent"')


def spec(code, group="played", text="rule"):
    return RuleSpec(rule_key(code), group, "events", ("played",), code, text)


def play(card, seat=0):
    return {"action": {"type": "play", "card": card}, "seat": seat,
            "events": [{"type": "played", "seat": seat, "card": card}], "view": make_view()}


def game(gid, cards, *, split="train", origin="corpus", experiment=None, start=1):
    prefix = "x" if origin == "probe" else "t"
    return make_game(gid, [play(c) for c in cards], split=split, origin=origin, prefix=prefix, start=start,
                     deck=cards + ["vent"] * 10, meta={"experiment_id": experiment} if experiment else None)


@pytest.fixture
def world():
    seed = [game("g-1", ["vent", "gear1", "scan"], start=1), game("g-2", ["gear2", "vent", "jam", "scan"], start=10),
            game("g-3", ["gear1", "gear3", "gear2", "vent"], split="heldout", start=20)]
    probes = [game("px-0001", ["jam", "vent", "gear1"], origin="probe", start=1, experiment="e-jam-draw"),
              game("px-0002", ["scan", "vent"], origin="probe", start=10),
              game("px-0003", ["gear2", "jam"], origin="probe", start=20, experiment="e-m1"),
              game("px-0004", ["vent", "vent"], origin="probe", start=30, experiment="e-reverse-3p")]
    return seed, probes


# ---------- replay ----------
def test_rules_come_from_the_ledger_whatever_their_status():
    ledger = Ledger()
    for i, code in enumerate((PLAYED, WRONG)):
        ledger.add_claim(text=f"r{i}", source=Source.MODEL, created_by="t", key=f"k{i}",
                         rule={"type": "executable", "group": "played", "kind": "events", "covers": ["played"], "code": code})
    ledger.add_claim(text="manual claim", source=Source.MANUAL, created_by="t", key="m", rule={"type": "x"})
    assert [r.text for r in rules_from_ledger(ledger)] == ["r0", "r1"]


def test_rescore_is_decided_by_the_scorer(world):
    seed, probes = world
    out = rescore([spec(PLAYED), spec(WRONG, text="wrong")], seed + probes)
    assert out.statuses == {rule_key(PLAYED): "confirmed", rule_key(WRONG): "refuted"}
    assert out.summary()["confirmed"] == 1 and out.groups_with_confirmed == 1


def test_rescore_never_changes_the_games_it_is_given(world):
    seed, probes = world
    before = [g.to_json() for g in seed + probes]
    rescore([spec(PLAYED)], seed + probes)
    assert [g.to_json() for g in seed + probes] == before


# ---------- T12.3 ----------
def test_zero_probes_confirms_nothing_and_probes_help(world):
    seed, probes = world
    curve = learning_curve([spec(PLAYED)], seed, probes, points=[0, 1, 2, 4])
    assert [r["k"] for r in curve] == [0, 1, 2, 4]
    assert curve[0]["confirmed"] == 0 and curve[0]["inferred"] == 1          # fits the traces, never tested
    assert curve[-1]["confirmed"] == 1 and baseline_is_low(curve)


def test_a_curve_that_never_improves_is_not_a_low_baseline(world):
    seed, _ = world
    assert not baseline_is_low(learning_curve([spec(PLAYED)], seed, [], points=[0]))


def test_points_beyond_the_probe_count_are_ignored(world):
    seed, probes = world
    assert [r["k"] for r in learning_curve([spec(PLAYED)], seed, probes, points=[0, 99])] == [0]


# ---------- T12.1 ----------
SEED_IDS = ["e-jam-draw", "e-reverse-3p"]


def test_orders_differ_only_in_what_comes_first(world):
    _, probes = world
    assert [g.game_id for g in order_with_artifacts(probes, SEED_IDS)] == ["px-0001", "px-0004", "px-0002", "px-0003"]
    assert [g.game_id for g in order_traces_only(probes, SEED_IDS)] == ["px-0002", "px-0003"]


def test_ablation_reports_same_rules_and_what_each_order_needs(world):
    seed, probes = world
    result = compare([spec(PLAYED), spec(WRONG, text="wrong")], seed, probes, SEED_IDS, [0, 1, 2, 4])
    assert result["same_confirmed_rules"] and result["only_with_artifacts"] == [] == result["only_traces_only"]
    assert result["with_artifacts"]["probes_to_settle"] == 1 == result["traces_only"]["probes_to_settle"]
    assert result["fewer_probes_with_artifacts"] is False                       # a tie is not a win


def test_a_difference_in_confirmed_rules_is_reported_not_hidden(world):
    seed, probes = world
    only_seeded = [probes[0], probes[3]]                                         # traces-only arm has no probe step for it
    result = compare([spec(GEAR_ONLY, text="gear rule")], seed, only_seeded, ["e-jam-draw", "e-reverse-3p"], [0, 1, 2])
    assert result["traces_only"]["final_confirmed"] == [] and len(result["with_artifacts"]["final_confirmed"]) == 1
    assert not result["same_confirmed_rules"] and len(result["only_with_artifacts"]) == 1


def test_coverage_counts_new_situations_only(world):
    seed, probes = world
    universe = {step_signature(s) for g in probes for s in g.steps} - {step_signature(s) for g in seed for s in g.steps}
    counts = coverage_curve(seed, probes, universe)
    assert counts == sorted(counts) and counts[-1] == len(universe)
    assert probes_to_reach(counts, len(universe)) == counts.index(len(universe)) + 1
    assert probes_to_reach([0, 1], 5) is None


# ---------- T12.2 ----------
def test_tokens_are_opaque_and_reversible(world):
    seed, probes = world
    tokens = TokenMap(card_names(seed + probes))
    assert set(tokens.forward.values()).isdisjoint(tokens.forward) and len(tokens.forward) == len(card_names(seed + probes))
    assert all(t.startswith("k") for t in tokens.forward.values())
    payload = {"hands": [["vent", "scan"]], "n": {"jam": 2}, "type": "play"}
    assert tokens.reveal(tokens.apply(payload)) == payload and "vent" not in str(tokens.apply(payload))


def test_relabeled_games_keep_their_ids_and_shape(world):
    seed, _ = world
    tokens = TokenMap(card_names(seed))
    masked = relabel_game(seed[0], tokens)
    assert masked.game_id == seed[0].game_id and [s["trace_id"] for s in masked.steps] == [s["trace_id"] for s in seed[0].steps]
    assert masked.steps[0]["action"]["card"] == tokens.forward["vent"] and masked.steps[0]["before"] is not None


def test_rule_literals_are_relabeled_exactly():
    tokens = TokenMap({"vent", "gear1", "gear10"})
    code = "if c == 'gear1' or c == \"gear10\" or c == 'vent':\n    note = 'gear1x'"
    out = tokens.rule_code(code)
    assert tokens.forward["gear1"] in out and tokens.forward["gear10"] in out and "'gear1x'" in out
    assert "'gear1'" not in out and "'vent'" not in out


def test_same_ledger_when_rules_depend_on_behavior_not_names(world):
    seed, probes = world
    result = opaque_check([spec(PLAYED), spec(WRONG, text="wrong")], seed + probes)
    assert result["same_ledger"] and result["changed"] == [] and result["tokens"] == 6
    assert result["plain"]["confirmed"] == result["opaque"]["confirmed"] == 1


def test_a_rule_that_leans_on_how_names_look_is_caught(world):
    seed, probes = world
    result = opaque_check([spec(GEAR_ONLY, text="gear rule")], seed + probes)
    assert not result["same_ledger"] and result["changed"][0]["before"] != result["changed"][0]["after"]


# ---------- the command ----------
def test_cli_writes_json_and_markdown(tmp_path, world, capsys):
    from auditor.main.corpus import save_games
    seed, probes = world
    run = tmp_path / "r-c"
    ledger = Ledger(run / "ledger.jsonl")
    ledger.add_claim(text="played rule", source=Source.MODEL, created_by="t", key="k",
                     rule={"type": "executable", "group": "played", "kind": "events", "covers": ["played"], "code": PLAYED})
    save_games(run / "probe_games.jsonl", probes)
    import controls.main.cli as cli
    cli.load_seed_corpus = lambda fs: seed
    try:
        assert controls_main(["--run-id", "r-c", "--runs-dir", str(tmp_path), "--dig-site", str(tmp_path)]) == 0
    finally:
        from auditor.main.corpus import load_seed_corpus
        cli.load_seed_corpus = load_seed_corpus
    out = (run / "controls.md").read_text()
    for heading in ("(T12.3)", "(T12.1)", "(T12.2)"):
        assert heading in out
    assert "Zero-probe baseline scores low" in out and "**yes**" in out
    assert (run / "controls.json").exists() and "do not re-run the Muse proposal step" in out


def test_cli_without_a_ledger_says_so(tmp_path, capsys):
    assert controls_main(["--run-id", "none", "--runs-dir", str(tmp_path)]) == 2
