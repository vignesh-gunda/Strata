"""Load API keys from the repo-root .env without ever echoing them."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader. Variables already in the environment win."""
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def require_key(name: str) -> str:
    """Return the named key or raise without revealing anything about it."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set in .env or the environment")
    return value
