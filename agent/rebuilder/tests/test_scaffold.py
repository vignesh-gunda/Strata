"""The rebuild folder appears as soon as a rebuild is asked for, and grows as the agent learns."""
from __future__ import annotations

import time

from coordinator.main.runs import FolderWriter, RunManager, StageResult
from ledger.main.models import Source, Status
from ledger.main.store import Ledger
from rebuilder.main.languages import GO, JAVASCRIPT
from rebuilder.main.scaffold import folder_for, is_starter, refresh, remove, scaffold

RULE = 'KIND = "events"\nCOVERS = ["drew"]\ndef predict(h, v, a, e):\n    return None\n'


def add_rule(ledger, group="drew", status=None, code=RULE, key="r1"):
    c = ledger.add_claim(text=f"[{group}] a rule", source=Source.MODEL, created_by="t", key=key,
                         rule={"type": "executable", "group": group, "kind": "events", "covers": ["drew"], "code": code})
    if status:
        ledger.set_status(c.id, status, ["t-000001"])
    return c


def test_scaffold_puts_each_section_in_its_own_folder(tmp_path):
    for lang in (GO, JAVASCRIPT):
        folder = scaffold(tmp_path, lang)
        assert {p.name for p in folder.iterdir()} == {"README.md", "program", "notes", "rules"}
        assert {lang.engine_file, *lang.support_files} <= {p.name for p in (folder / "program").iterdir()}
        assert (folder / "notes" / "GAME_NOTES.md").exists()
        assert is_starter(folder, lang)
        engine = (folder / "program" / lang.engine_file).read_text()
        assert engine.count("nknown(") >= 4 and "setup" in engine
        assert "| `program/` |" in (folder / "README.md").read_text()


def test_go_starter_is_formatted_and_one_unknown_per_line(tmp_path):
    engine = (scaffold(tmp_path, GO) / "program" / "engine.go").read_text()
    assert '\t"setup",\n\t"legal",' in engine and "\tUnknown(\"how a game is set up\")" in engine


def test_scaffold_never_overwrites_a_real_engine(tmp_path):
    program = folder_for(tmp_path, GO) / "program"
    program.mkdir(parents=True)
    (program / "engine.go").write_text("package main\n// written by the rebuild\n")
    scaffold(tmp_path, GO)
    assert (program / "engine.go").read_text().endswith("written by the rebuild\n")
    assert not is_starter(folder_for(tmp_path, GO), GO)


def test_notes_and_one_file_per_rule_follow_the_ledger(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    auth = ledger.grant_scorer()
    manual = ledger.add_claim(text="Vent lowers pressure by 3.", source=Source.MANUAL, created_by="t", key="m")
    ledger.set_status(manual.id, Status.REFUTED, ["t-000009"], authority=auth)
    ledger.add_finding(claim_id=manual.id, artifact="manual", artifact_said="Vent lowers pressure by 3.",
                       traces_said="vent lowers by 4", trace_ids=["t-000009"], kind="stale")
    ledger.add_claim(text="What jam does", source=Source.CODE, created_by="t", key="o")
    assert refresh(tmp_path, GO) == ["notes/GAME_NOTES.md"]
    notes = (folder_for(tmp_path, GO) / "notes" / "GAME_NOTES.md").read_text()
    assert 'The manual said "Vent lowers pressure by 3". The games show: vent lowers by 4 (trace t-000009).' in notes
    assert "## Still unknown" in notes and "What jam does" in notes

    rule = add_rule(ledger, status=Status.INFERRED)
    assert refresh(tmp_path, GO) == ["notes/GAME_NOTES.md", f"rules/drew/likely-{rule.id}.py"]
    text = (folder_for(tmp_path, GO) / "rules" / "drew" / f"likely-{rule.id}.py").read_text()
    assert text.startswith(f"# Rule {rule.id} about 'drew' in Pressure.")
    assert "# Status: likely, not yet proven." in text and 'COVERS = ["drew"]' in text
    assert refresh(tmp_path, GO) == []                         # nothing changed: nothing rewritten

    ledger.set_status(rule.id, Status.REFUTED, ["t-000002"], authority=auth)
    changed = refresh(tmp_path, GO)
    assert f"rules/drew/wrong-{rule.id}.py" in changed and f"rules/drew/likely-{rule.id}.py (removed)" in changed
    assert [p.name for p in (folder_for(tmp_path, GO) / "rules" / "drew").iterdir()] == [f"wrong-{rule.id}.py"]


def test_rule_code_with_restricted_text_is_left_out(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    add_rule(ledger, code=RULE + "# see services/pressure/sealed\n")
    refresh(tmp_path, GO)
    assert not list((folder_for(tmp_path, GO) / "rules").rglob("*.py"))


def test_starting_a_rebuild_creates_the_folder_before_any_stage_runs(tmp_path):
    seen = []

    def runner(module, run_id, runs_dir, *extra):
        if not seen:
            seen.append(sorted(p.name for p in (folder_for(tmp_path / "r-s", GO) / "program").iterdir()))
        return StageResult(0)

    manager = RunManager(tmp_path, "r-s", lambda m: None, stage_runner=runner, sleep=lambda s: None)
    manager.start(GO)
    manager.join(10)
    assert {"engine.go", "server.go", "go.mod"} <= set(seen[0])


def test_folder_writer_rewrites_while_the_run_works(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    writer = FolderWriter(tmp_path, GO, poll=0.05).start()
    rule = add_rule(ledger, group="played", key="p", status=Status.INFERRED)
    target = folder_for(tmp_path, GO) / "rules" / "played" / f"likely-{rule.id}.py"
    deadline = time.time() + 3
    while not target.exists() and time.time() < deadline:
        time.sleep(0.05)
    writer.stop()
    assert target.exists()


# ---------- stopping removes the code ----------
def test_stopping_a_run_removes_the_code_it_was_writing(tmp_path):
    import threading
    gate, posted = threading.Event(), []

    def runner(module, run_id, runs_dir, *extra):
        gate.wait(5)
        return StageResult(1, "cancelled by you")

    manager = RunManager(tmp_path, "r-x", posted.append, stage_runner=runner, sleep=lambda s: None, stage_attempts=1)
    manager.start(GO)
    folder = folder_for(tmp_path / "r-x", GO)
    assert (folder / "program" / "engine.go").exists()
    manager.request_stop()
    gate.set()
    manager.join(10)
    assert not folder.exists() and not folder.parent.exists()
    assert "removed the code" in posted[-1]


def test_stopping_when_nothing_runs_removes_the_folder_at_once(tmp_path):
    manager = RunManager(tmp_path, "r-y", lambda m: None, sleep=lambda s: None)
    scaffold(tmp_path / "r-y", JAVASCRIPT)
    manager.request_stop()
    assert not folder_for(tmp_path / "r-y", JAVASCRIPT).exists()


def test_a_run_that_is_not_stopped_keeps_its_code(tmp_path):
    manager = RunManager(tmp_path, "r-z", lambda m: None, sleep=lambda s: None,
                         stage_runner=lambda *a: StageResult(0))
    manager.start(GO)
    manager.join(10)
    assert (folder_for(tmp_path / "r-z", GO) / "program" / "engine.go").exists()


def test_remove_only_touches_that_language(tmp_path):
    scaffold(tmp_path, GO)
    scaffold(tmp_path, JAVASCRIPT)
    assert remove(tmp_path, GO) and not remove(tmp_path, GO)
    assert folder_for(tmp_path, JAVASCRIPT).exists()


# ---------- where the rebuild goes ----------
def test_real_runs_rebuild_at_the_top_of_the_project(monkeypatch):
    from rebuilder.main.location import AGENT_ROOT, REPO_ROOT, rebuild_dir
    monkeypatch.delenv("STRATA_REBUILD_ROOT", raising=False)
    assert rebuild_dir(AGENT_ROOT / "runs" / "r-go") == REPO_ROOT / "rebuild" / "r-go"
    assert folder_for(AGENT_ROOT / "runs" / "r-go", GO) == REPO_ROOT / "rebuild" / "r-go" / "go"
    from rebuilder.main.scaffold import program_dir
    assert program_dir(AGENT_ROOT / "runs" / "r-go", GO) == REPO_ROOT / "rebuild" / "r-go" / "go" / "program"


def test_runs_kept_elsewhere_keep_their_rebuild_inside_the_run(tmp_path, monkeypatch):
    from rebuilder.main.location import rebuild_dir
    monkeypatch.delenv("STRATA_REBUILD_ROOT", raising=False)
    assert rebuild_dir(tmp_path / "r-x") == tmp_path / "r-x" / "rebuild"


def test_the_location_can_be_overridden(tmp_path, monkeypatch):
    from rebuilder.main.location import rebuild_dir
    monkeypatch.setenv("STRATA_REBUILD_ROOT", str(tmp_path / "out"))
    assert rebuild_dir("/anywhere/runs/r-1") == tmp_path / "out" / "r-1"
