"""Tests for the batch simulator that records evidence traces."""

import json
from collections import Counter

import pytest

from blackbox.scripts import script_from_seed
from simulate.policies import POLICIES
from simulate.run import RunConfig, build_specs, run
from simulate.scenarios import TEMPLATES, arrange


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_arrange_places_cards_and_keeps_deck_composition():
    deck = script_from_seed(1)["deck"]
    arranged = arrange(deck, 3, {0: ["jam", "double"], 2: ["scan"]}, ["gear3"])
    assert Counter(arranged) == Counter(deck)
    assert arranged[0] == "jam" and arranged[3] == "double"  # seat 0, cards 1 and 2
    assert arranged[2] == "scan"  # seat 2, card 1
    assert arranged[12] == "gear3"  # top of the draw pile


def test_arrange_rejects_impossible_requests():
    deck = script_from_seed(1)["deck"]
    with pytest.raises(ValueError):
        arrange(deck, 2, {0: ["scan"] * 4, 1: ["scan"]}, [])  # only 3 scans exist
    with pytest.raises(ValueError):
        arrange(deck, 2, {0: ["vent"] * 5}, [])


def test_specs_mix_scenarios_policies_and_heldout():
    specs = build_specs(RunConfig(games=400, seed=42))
    assert len(specs) == 400
    kinds = Counter(spec.label.split(":")[0] for spec in specs)
    assert kinds == {"scenario": 80, "policy": 320}
    heldout = [spec for spec in specs if spec.split == "heldout"]
    assert len(heldout) == 80
    assert {spec.label.split(":")[0] for spec in heldout} == {"scenario", "policy"}
    assert {spec.label for spec in specs} >= {f"scenario:{name}" for name in TEMPLATES}
    assert {spec.label for spec in specs} >= {f"policy:{name}" for name in POLICIES}


def test_run_writes_traces_games_and_manifest(tmp_path):
    config = RunConfig(games=10, seed=7, mutations=2, run_id="r-test", out=tmp_path)
    run_dir = run(config)
    traces = read_jsonl(run_dir / "traces.jsonl")
    games = read_jsonl(run_dir / "games.jsonl")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert len(games) == 10 and manifest["games"] == 10 and manifest["traces"] == len(traces)
    assert len({trace["trace_id"] for trace in traces}) == len(traces)
    first = traces[0]
    assert set(first) >= {"trace_id", "run_id", "game_id", "step", "seat", "turn", "action", "legal", "events", "view"}
    assert first["prediction"] is None and first["surprise"] is None
    assert all(game["steps"] == sum(t["game_id"] == game["game_id"] for t in traces) for game in games)
    assert "mutations" not in json.dumps(manifest) or manifest["mutations"] == "hidden"
    assert not (run_dir / "sealed.json").exists()


def test_scenario_openings_are_played_first(tmp_path):
    run_dir = run(RunConfig(games=5, seed=3, run_id="r-open", out=tmp_path))
    traces = read_jsonl(run_dir / "traces.jsonl")
    games = read_jsonl(run_dir / "games.jsonl")
    scenario = next(game for game in games if game["label"].startswith("scenario:"))
    opening = TEMPLATES[scenario["label"].split(":")[1]].opening
    steps = [t for t in traces if t["game_id"] == scenario["game_id"]][: len(opening)]
    assert [step["action"] for step in steps] == list(opening)


def test_runs_are_reproducible(tmp_path):
    first = run(RunConfig(games=6, seed=11, mutations=1, run_id="r-a", out=tmp_path))
    second = run(RunConfig(games=6, seed=11, mutations=1, run_id="r-b", out=tmp_path))
    strip = lambda rows: [{k: v for k, v in row.items() if k != "run_id"} for row in rows]
    assert strip(read_jsonl(first / "traces.jsonl")) == strip(read_jsonl(second / "traces.jsonl"))


def test_sealed_file_only_when_asked(tmp_path):
    sealed = tmp_path / "sealed" / "r-s.json"
    run(RunConfig(games=2, seed=5, mutations=2, run_id="r-s", out=tmp_path, sealed_out=sealed))
    assert len(json.loads(sealed.read_text())["mutations"]) == 2


def test_live_mode_prints_every_step_and_game(tmp_path, capsys):
    run_dir = run(RunConfig(games=3, seed=4, run_id="r-live", out=tmp_path, live=True))
    printed = capsys.readouterr().out.splitlines()
    traces = read_jsonl(run_dir / "traces.jsonl")
    assert sum(line.startswith("t-") for line in printed) == len(traces)
    assert sum(line.startswith("== ") for line in printed) == 3
    assert json.loads((run_dir / "manifest.json").read_text())["status"] == "complete"


def test_forever_mode_streams_until_interrupted(tmp_path, monkeypatch):
    from simulate import run as run_module

    calls = {"games": 0}
    real_play = run_module.play_game

    def play_then_stop(*args, **kwargs):
        calls["games"] += 1
        if calls["games"] > 25:
            raise KeyboardInterrupt
        return real_play(*args, **kwargs)

    monkeypatch.setattr(run_module, "play_game", play_then_stop)
    run_dir = run(RunConfig(games=1, seed=6, run_id="r-forever", out=tmp_path, forever=True))
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "stopped" and manifest["games"] == 25
    assert len(read_jsonl(run_dir / "games.jsonl")) == 25


def test_opaque_run_uses_only_random_policy(tmp_path):
    run_dir = run(RunConfig(games=5, seed=2, opaque=True, run_id="r-op", out=tmp_path))
    games = read_jsonl(run_dir / "games.jsonl")
    assert {game["label"] for game in games} == {"policy:random"}
    text = (run_dir / "traces.jsonl").read_text()
    assert '"gear1"' not in text and '"vent"' not in text
