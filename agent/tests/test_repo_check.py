"""T13.2: the pre-publish check catches what it is meant to catch."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_repo.py"
spec = importlib.util.spec_from_file_location("check_repo", SCRIPT)
check_repo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_repo)


def make_repo(tmp_path, monkeypatch, gitignore, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(gitignore)
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "add", "-f", "."], cwd=tmp_path, check=True)
    monkeypatch.setattr(check_repo, "REPO", tmp_path)
    return tmp_path


FULL_IGNORE = ".env\nservices/pressure/sealed/runs/\ndig-site/bin/\ndig-site/MANIFEST.json\ndig-site/traces/\nagent/runs/\n.venv/\n"


def test_this_repository_passes():
    assert all(not problems for problems in check_repo.run_checks().values())


def test_a_missing_ignore_rule_is_reported(tmp_path, monkeypatch):
    make_repo(tmp_path, monkeypatch, FULL_IGNORE.replace("dig-site/bin/\n", ""), {"README.md": "x"})
    assert any("dig-site/bin/pressure.pyz is NOT ignored" in p for p in check_repo.check_ignored())


def test_a_tracked_private_file_is_reported(tmp_path, monkeypatch):
    make_repo(tmp_path, monkeypatch, FULL_IGNORE, {".env": "META_API_KEY=abc", "dig-site/bin/pressure.pyz": "x"})
    problems = check_repo.check_not_tracked(check_repo.tracked_files())
    assert any(".env is tracked" in p for p in problems) and any("dig-site/bin" in p for p in problems)


def test_a_key_value_in_a_tracked_file_is_reported_without_printing_it(tmp_path, monkeypatch):
    secret = "abcd1234efgh5678"
    make_repo(tmp_path, monkeypatch, FULL_IGNORE, {".env": f"META_API_KEY={secret}\n", "notes.txt": f"key {secret}"})
    problems = check_repo.check_no_secrets(["notes.txt"])
    assert problems == ["notes.txt contains a value from .env"] and secret not in " ".join(problems)


def test_key_shaped_strings_are_reported(tmp_path, monkeypatch):
    make_repo(tmp_path, monkeypatch, FULL_IGNORE, {"a.py": "TOKEN = 'sk-abcdefghijklmnopqrstuv'"})
    assert check_repo.check_no_secrets(["a.py"]) == ["a.py contains a key-shaped string"]


def test_agent_code_may_not_point_at_the_original_service(tmp_path, monkeypatch):
    make_repo(tmp_path, monkeypatch, FULL_IGNORE, {
        "agent/x/main/bad.py": "open('services/pressure/sealed/RULES.md')",
        "agent/llm/main/firewall.py": "services/pressure",           # allowed: it is the firewall
        "agent/x/main/doc.py": '"""Anything outside dig-site/ (notably `services/`) is unreachable."""',
        "agent/x/tests/test_ok.py": "services/pressure"})
    assert check_repo.check_agent_isolation(check_repo.tracked_files()) == ["agent/x/main/bad.py refers to the original service"]
