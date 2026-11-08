#!/usr/bin/env python3
"""Pre-publish checks for the repository. Stdlib only; prints PASS/FAIL, never a secret.

    python3 scripts/check_repo.py

1. The files that must stay private are ignored by git and not tracked.
2. No tracked file contains a value from .env or a key-shaped string.
3. Nothing under agent/ refers to the original service, except the few places that must
   (the prompt firewall that blocks it, and the report step that reveals the answer key).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Paths that must be ignored (a sample file name under each is enough for `git check-ignore`).
MUST_BE_IGNORED = [
    ".env",
    "services/pressure/sealed/runs/r-01.json",
    "dig-site/bin/pressure.pyz",
    "dig-site/MANIFEST.json",
    "dig-site/traces/seed-corpus/traces.jsonl",
    "agent/runs/r-01/ledger.jsonl",
    "rebuild/r-01/go/engine.go",
    "agent/.venv/bin/python",
]
# Tracked paths that must never appear.
MUST_NOT_BE_TRACKED = re.compile(r"(^|/)\.env($|\.)|sealed/runs/|dig-site/bin/|dig-site/MANIFEST\.json|agent/runs/|^rebuild/|\.venv/")
KEY_SHAPES = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9._-]{24,}|\b[A-Fa-f0-9]{40,}\b")
# Files allowed to mention the original service by path.
MAY_MENTION_SERVICES = {
    "agent/llm/main/firewall.py",            # blocks it
    "agent/report/main/cli.py",              # the post-run reveal reads the answer key from here
    "agent/report/main/reveal.py",
    "agent/tasks.md",
    "agent/concierge/main/scope.py",         # explains the refusal
}
SKIP_DIRS = ("/tests/", "/.venv/", "/__pycache__/")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def tracked_files() -> list[str]:
    return [p for p in git("ls-files").stdout.splitlines() if p]


def check_ignored() -> list[str]:
    bad = []
    for path in MUST_BE_IGNORED:
        if git("check-ignore", "-q", path).returncode != 0:
            bad.append(f"{path} is NOT ignored by git")
    return bad


def check_not_tracked(files: list[str]) -> list[str]:
    return [f"{p} is tracked but must stay private" for p in files if MUST_NOT_BE_TRACKED.search(p)]


def env_values() -> list[str]:
    env = REPO / ".env"
    if not env.exists():
        return []
    values = []
    for line in env.read_text().splitlines():
        name, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if name.strip() and len(value) >= 8:
            values.append(value)
    return values


def check_no_secrets(files: list[str]) -> list[str]:
    secrets, bad = env_values(), []
    for rel in files:
        path = REPO / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if any(v in text for v in secrets):
            bad.append(f"{rel} contains a value from .env")
        elif KEY_SHAPES.search(text) and not rel.endswith((".jsonl", ".json", ".svg", ".html")):
            bad.append(f"{rel} contains a key-shaped string")
    return bad


def check_agent_isolation(files: list[str]) -> list[str]:
    bad = []
    for rel in files:
        if not rel.startswith("agent/") or rel in MAY_MENTION_SERVICES or not rel.endswith((".py", ".js", ".go", ".md")):
            continue
        if any(s in "/" + rel for s in SKIP_DIRS):
            continue
        text = (REPO / rel).read_text(errors="ignore")
        if re.search(r"services[/\\]\w", text):
            bad.append(f"{rel} refers to the original service")
    return bad


def run_checks() -> dict[str, list[str]]:
    files = tracked_files()
    return {"private files are ignored": check_ignored(),
            "private files are not tracked": check_not_tracked(files),
            "no secrets in tracked files": check_no_secrets(files),
            "agent never refers to the original service": check_agent_isolation(files)}


def main() -> int:
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("Not a git repository.")
        return 2
    failed = 0
    for name, problems in run_checks().items():
        print(f"{'PASS' if not problems else 'FAIL'}  {name}")
        for problem in problems:
            print(f"        {problem}")
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
