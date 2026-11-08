"""T3 tests: the survey reads the dig-site, plays nothing, and ranks what to probe."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ledger.main.models import Source, Status, Tier
from ledger.main.store import Ledger, LedgerError
from llm.main.muse import MuseClient
from sandbox.main.digsite_fs import DigSiteFS
from surveyor.main.code import survey_code
from surveyor.main.manual import parse_manual
from surveyor.main.muse_extra import muse_extra_claims
from surveyor.main.probes import CLASSES, build_probe_list, priority_class
from surveyor.main.survey import main, run_survey

REAL = Path(__file__).resolve().parents[3] / "dig-site"
pytestmark = pytest.mark.skipif(not (REAL / "source" / "engine.py").exists(),
                                reason="dig-site source/ and docs/ not present")


@pytest.fixture(scope="module")
def manual_text():
    return DigSiteFS(REAL).read_text("docs/pressure-manual-v1.md")


@pytest.fixture(scope="module")
def code_files():
    fs = DigSiteFS(REAL)
    return {p: fs.read_text(p) for p in ("source/engine.py", "source/state.py")}


@pytest.fixture()
def site_copy(tmp_path):
    """dig-site docs + source only: no binary, no manifest, no corpus."""
    root = tmp_path / "dig-site"
    shutil.copytree(REAL / "docs", root / "docs")
    shutil.copytree(REAL / "source", root / "source")
    return root


def rules_of(specs):
    return {s.key: s.rule for s in specs if s.rule}


# ---------- manual ----------

def test_manual_claims_match_what_the_manual_says(manual_text):
    rules = rules_of(parse_manual(manual_text))
    assert rules["card:vent:pressure_delta"] == {"card": "vent", "type": "pressure_delta", "value": -3}
    assert rules["card:vent:pressure_floor"]["value"] == 0
    assert rules["card:gear1:pressure_delta"]["value"] == 1
    assert rules["card:gear3:pressure_delta"]["value"] == 3
    assert rules["card:scan:scan_depth"]["value"] == 3
    assert rules["card:double:double"]["value"] == 2
    assert rules["turn:hand_limit"] == {"type": "hand_limit", "value": 7}
    assert rules["overflow:limit"]["value"] == 12
    assert rules["overflow:reset"]["value"] == 0
    assert rules["setup:deal"]["value"] == 4 and rules["setup:start_pressure"]["value"] == 0
    assert rules["setup:players"] == {"type": "players_range", "min": 2, "max": 4}
    assert "win:last_standing" in rules and "turn:pass_only_when_stuck" in rules
    assert "card:reverse:reverse" in rules and "card:jam:jam" in rules


def test_every_card_in_the_manual_gets_a_claim(manual_text):
    topics = {s.topic for s in parse_manual(manual_text)}
    for card in ("gear1", "gear2", "gear3", "vent", "reverse", "double", "jam", "scan"):
        assert f"card:{card}" in topics


def test_unrecognized_table_row_is_kept_not_dropped():
    text = "## 4. The cards\n| Card | Effect |\n|---|---|\n| Zap | Does something odd. |\n"
    specs = parse_manual(text)
    assert [s.key for s in specs] == ["card:zap:raw"] and specs[0].rule is None


def test_manual_parsing_is_deterministic(manual_text):
    assert [(s.key, s.text) for s in parse_manual(manual_text)] == \
           [(s.key, s.text) for s in parse_manual(manual_text)]


# ---------- code ----------

def test_code_survey_finds_the_unknowns(code_files):
    result = survey_code(code_files)
    assert sorted(result.missing_functions) == sorted(
        ["_gear", "_vent", "_reverse", "_double", "_jam", "_draw"])
    assert result.parameters == ["hand_limit", "hand_size", "scan_depth"]
    assert sorted(result.hidden_state) == ["direction", "extra_actions", "jammed", "skip_next"]
    assert set(result.missing_modules) == {"blackbox.cards", "blackbox.rules"}
    assert "ruleset_for" in result.missing_modules["blackbox.rules"]
    assert "scan_depth" not in result.missing_functions and "_scan" not in result.missing_functions


def test_code_claims_come_from_the_code_that_exists(code_files):
    keys = {spec.key for spec, _ in survey_code(code_files).claims}
    assert {"draw_offered", "stalemate", "winner_null", "scan_reveals", "skip_next",
            "extra_actions", "jam_clears", "initial:pressure"} <= keys


def test_code_claims_state_no_rule_values(code_files):
    """rules.py is absent, so no code claim may carry a number for a parameter."""
    for spec, _ in survey_code(code_files).claims:
        if spec.key in ("draw_offered", "scan_reveals", "deal_round_robin"):
            assert "value" not in spec.rule


def test_hidden_state_uses_view_keys_when_given(code_files):
    # If the view did show `pressure`'s sibling keys, they would not be hidden.
    shown = {"hands", "alive", "draw_pile_size", "discard", "pressure", "current_seat",
             "actions_left", "turn", "over", "winner", "direction"}
    assert "direction" in survey_code(code_files).hidden_state          # FIELD_VIEW says hidden
    assert "pressure" not in survey_code(code_files, view_keys=shown).hidden_state
    assert "pressure" in survey_code(code_files, view_keys=set()).hidden_state


def test_function_that_is_not_lost_is_not_reported():
    src = ("class GameState:\n    pass\n"
           "def lost():\n    raise NotImplementedError('x')\n"
           "def lost_with_doc():\n    '''doc'''\n    raise NotImplementedError\n"
           "def fine():\n    raise ValueError('real error')\n"
           "def also_fine():\n    return 1\n")
    result = survey_code({"source/x.py": src})
    assert sorted(result.missing_functions) == ["lost", "lost_with_doc"]


# ---------- the whole survey ----------

@pytest.fixture()
def surveyed(site_copy, tmp_path):
    ledger = Ledger(tmp_path / "run" / "ledger.jsonl")
    result = run_survey(DigSiteFS(site_copy), ledger, run_dir=tmp_path / "run")
    return ledger, result, tmp_path / "run"


def test_survey_adds_no_confirmed_or_refuted_claims(surveyed):
    ledger, result, _ = surveyed
    counts = ledger.counts()
    assert counts["confirmed"] == counts["refuted"] == counts["observed"] == counts["inferred"] == 0
    assert counts["open"] == len(ledger.all_claims()) > 40
    assert result.summary["games_played"] == 0


def test_every_claim_has_a_source_a_tier_and_an_author(surveyed):
    ledger, _, _ = surveyed
    for claim in ledger.all_claims():
        assert claim.source in (Source.MANUAL, Source.CODE)
        assert claim.tier is (Tier.DOCUMENTED if claim.source is Source.MANUAL
                              else Tier.READ_FROM_CODE)
        assert claim.created_by == "surveyor" and claim.text and claim.key
        assert claim.evidence == []
    assert ledger.by_tier("documented") and ledger.by_tier("read_from_code")


def test_survey_plays_no_game_and_starts_no_process(site_copy, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the survey must not start any process")
    monkeypatch.setattr(subprocess, "Popen", boom)
    run_survey(DigSiteFS(site_copy), Ledger(), run_dir=tmp_path / "r")


def test_survey_cannot_confirm_anything_even_if_asked(surveyed):
    ledger, _, _ = surveyed
    claim = ledger.by_source("manual")[0]
    with pytest.raises(LedgerError, match="only the scorer"):
        ledger.set_status(claim.id, "confirmed", ["t-000001"])


def test_probe_list_puts_missing_functions_then_parameters_first(surveyed):
    _, result, _ = surveyed
    probes = result.probe_list
    classes = [p["priority_class"] for p in probes]
    assert classes == sorted(classes)                       # never out of order
    assert classes[:6] == [0] * 6 and classes[6:9] == [1] * 3
    assert {p["topic"] for p in probes[:6]} == {f"function:{n}" for n in
            ("_gear", "_vent", "_reverse", "_double", "_jam", "_draw")}
    assert {p["topic"] for p in probes[6:9]} == {"param:hand_limit", "param:hand_size",
                                                 "param:scan_depth"}
    assert [p["rank"] for p in probes] == list(range(1, len(probes) + 1))
    # Hint-only behaviors come before the plain documented claims.
    first_documented = classes.index(3)
    assert all(c == 2 for c in classes[9:first_documented])
    assert {"ending:stalemate", "turn:skip"} <= {p["topic"] for p in probes
                                                  if p["class_name"] == "hint_only"}


def test_probe_items_link_lost_code_to_the_claims_to_test(surveyed):
    ledger, result, _ = surveyed
    by_topic = {p["topic"]: p for p in result.probe_list}
    vent = by_topic["function:_vent"]
    related_texts = [ledger.get(i).text for i in vent["related_claim_ids"]]
    assert any("lowers the pressure gauge by 3" in t for t in related_texts)
    gear = by_topic["function:_gear"]
    assert len(gear["related_claim_ids"]) >= 3               # gear1, gear2, gear3
    assert by_topic["param:hand_limit"]["related_claim_ids"]
    for item in result.probe_list[:9]:
        assert item["experiment_hint"] and item["reason"]
    assert "double" in by_topic["function:_double"]["experiment_hint"]


def test_priority_class_names_line_up(surveyed):
    ledger, _, _ = surveyed
    assert CLASSES[0] == "missing_function" and CLASSES[1] == "unknown_parameter"
    stale = ledger.by_topic("function:_gear")[0]
    assert priority_class(stale) == 0


def test_survey_is_idempotent(site_copy, tmp_path):
    path = tmp_path / "run" / "ledger.jsonl"
    fs = DigSiteFS(site_copy)
    first = run_survey(fs, Ledger(path), run_dir=tmp_path / "run")
    again = run_survey(fs, Ledger(path), run_dir=tmp_path / "run")   # reloaded from disk
    assert again.summary["claims"] == first.summary["claims"]
    assert [p["claim_key"] for p in again.probe_list] == [p["claim_key"] for p in first.probe_list]


def test_survey_writes_the_run_files(surveyed):
    _, result, run_dir = surveyed
    assert json.loads((run_dir / "probe_list.json").read_text()) == result.probe_list
    summary = json.loads((run_dir / "survey.json").read_text())
    assert summary["unknowns"]["missing_functions"] and summary["probe_count"] == len(result.probe_list)
    assert summary["binary"] is None                      # no binary in the stripped copy
    assert (run_dir / "ledger.jsonl").stat().st_size > 0
    assert len(Ledger(run_dir / "ledger.jsonl").all_claims()) == summary["claims"]


@pytest.mark.skipif(not (REAL / "bin" / "pressure.pyz").exists(), reason="dig-site not built")
def test_survey_records_binary_metadata_only():
    result = run_survey(DigSiteFS(REAL), Ledger())
    binary = result.summary["binary"]
    assert binary["matches_manifest"] is True and binary["bytes"] > 0
    assert set(binary) >= {"sha256", "bytes", "entry_point"}
    assert result.summary["games_played"] == 0


def test_cli_prints_a_summary_and_writes_files(site_copy, tmp_path, capsys):
    code = main(["--dig-site", str(site_copy), "--run-id", "r-test", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "missing_function" in out and "claims" in out
    assert (tmp_path / "r-test" / "probe_list.json").exists()


def test_survey_never_reads_outside_the_dig_site(site_copy, tmp_path, monkeypatch):
    opened = []
    real_read = Path.read_text

    def spy(self, *a, **k):
        opened.append(str(self.resolve()))
        return real_read(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", spy)
    run_survey(DigSiteFS(site_copy), Ledger())
    root = str(site_copy.resolve())
    assert opened and all(p.startswith(root) for p in opened)
    assert not any("services" in p for p in opened)


# ---------- optional Muse pass ----------

class FakeTransport:
    def __init__(self, *texts):
        self.texts, self.requests = list(texts), []

    def __call__(self, url, headers, body, timeout):
        self.requests.append(body)
        text = self.texts.pop(0)
        return 200, {"output": [{"type": "message",
                                 "content": [{"type": "output_text", "text": text}]}],
                     "usage": {"total_tokens": 5}}


def test_muse_extra_claims_are_open_attributed_and_deduplicated(site_copy):
    ledger = Ledger()
    fs = DigSiteFS(site_copy)
    run_survey(fs, ledger)
    before = len(ledger.all_claims())
    reply = json.dumps({"claims": [
        {"text": "Gear cards add heat.", "topic": "card:gear1",
         "rule": {"type": "gear_adds_heat"}},
        {"text": "A player gets one action per turn.", "topic": "turn:actions"},   # duplicate
        {"text": "Unclear note", "rule": {"no_type": 1}},                          # rule dropped
        {"text": "  "},                                                            # empty
    ]})
    transport = FakeTransport(reply)
    client = MuseClient("k", transport=transport, sleep=lambda s: None)
    added = muse_extra_claims(client, ledger, artifact_text="manual text",
                              artifact_path="docs/pressure-manual-v1.md", source=Source.MANUAL)
    assert len(added) == 2 and len(ledger.all_claims()) == before + 2
    first = ledger.get(added[0])
    assert first.status is Status.OPEN and first.created_by == "muse"
    assert first.source is Source.MANUAL and first.tier is Tier.DOCUMENTED
    assert first.rule == {"type": "gear_adds_heat"} and ledger.get(added[1]).rule is None
    # The prompt carried only the artifact and the existing claims.
    assert "Claims already extracted" in transport.requests[0]["input"]


def test_run_survey_with_muse_adds_claims_but_no_status_change(site_copy):
    reply = json.dumps({"claims": [{"text": "Something new about the manual."}]})
    client = MuseClient("k", transport=FakeTransport(reply, '{"claims": []}', '{"claims": []}'),
                        sleep=lambda s: None)
    ledger = Ledger()
    result = run_survey(DigSiteFS(site_copy), ledger, muse=client)
    assert len(result.summary["muse_added"]) == 1
    assert ledger.counts()["confirmed"] == 0 and ledger.counts()["inferred"] == 0
