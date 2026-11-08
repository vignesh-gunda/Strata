"""T16.5, T16.8: a read-only window onto the folder the agent is writing code into.

Only files under one root (the run's rebuild folder, `<repo>/rebuild/<run_id>/`) can be listed or read. The jail
refuses `..`, absolute paths, symlinks, hidden files, secrets and anything that does not
look like plain text, and it runs file contents through the prompt firewall before they
are shown, so a restricted string can never reach the page.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

MAX_FILE_BYTES = 200_000
MAX_FILES = 200
SKIP_DIRS = {"node_modules", "__pycache__", ".git"}
DENIED_NAMES = {".env", "id_rsa", "id_ed25519"}
DENIED_PARTS = {"services", "sealed", "dig-site"}


class PathRefused(ValueError):
    """The requested path is outside the jail or not allowed."""


def _hidden(name: str) -> bool:
    return name.startswith(".") or name in DENIED_NAMES


def _is_text(sample: bytes) -> bool:
    if b"\0" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as error:
        return error.start >= len(sample) - 3     # a cut-off multi-byte character is still text
    return True


class FileJail:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---------- listing ----------
    def list(self) -> list[dict]:
        if not self.root.is_dir() or self.root.is_symlink():
            return []
        found: list[dict] = []
        for current, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _hidden(d) and d not in SKIP_DIRS
                             and not Path(current, d).is_symlink())
            for name in sorted(names):
                path = Path(current, name)
                if _hidden(name) or path.is_symlink():
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                with path.open("rb") as handle:
                    text = _is_text(handle.read(2048))
                found.append({"path": path.relative_to(self.root).as_posix(), "size": info.st_size,
                              "mtime": info.st_mtime_ns, "text": text})
                if len(found) >= MAX_FILES:
                    return found
        return found

    def fingerprint(self) -> dict[str, tuple[int, int]]:
        return {item["path"]: (item["size"], item["mtime"]) for item in self.list()}

    # ---------- reading ----------
    def resolve(self, rel: object) -> Path:
        if not isinstance(rel, str) or not rel or "\0" in rel or len(rel) > 300:
            raise PathRefused("that path is not allowed")
        if rel.startswith(("/", "\\")) or ":" in rel.split("/")[0]:
            raise PathRefused("that path is not allowed")
        parts = rel.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise PathRefused("that path is not allowed")
        if any(_hidden(part) or part in DENIED_PARTS for part in parts):
            raise PathRefused("that path is not allowed")
        current = self.root
        for part in parts:                              # no symlink anywhere on the way down
            current = current / part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise PathRefused("that path is not allowed")
            except FileNotFoundError:
                raise PathRefused("no such file") from None
        root = self.root.resolve()
        resolved = current.resolve()
        if root != resolved and root not in resolved.parents:
            raise PathRefused("that path is not allowed")
        if not resolved.is_file():
            raise PathRefused("no such file")
        return resolved

    def read(self, rel: object) -> dict:
        from llm.main import firewall

        path = self.resolve(rel)
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES)
        if not _is_text(data[:2048]):
            return {"path": rel, "size": size, "text": None, "binary": True, "truncated": False, "hidden": False}
        text = data.decode("utf-8", errors="replace")
        try:
            firewall.check(text)
        except firewall.PromptFirewallError:
            return {"path": rel, "size": size, "text": None, "binary": False, "truncated": False, "hidden": True}
        return {"path": rel, "size": size, "text": text, "binary": False,
                "truncated": size > MAX_FILE_BYTES, "hidden": False}


def diff_fingerprints(before: dict, after: dict) -> list[dict]:
    """What changed between two fingerprints: created, changed or deleted paths."""
    changes = []
    for path, marker in after.items():
        if path not in before:
            changes.append({"path": path, "kind": "created"})
        elif before[path] != marker:
            changes.append({"path": path, "kind": "changed"})
    changes.extend({"path": path, "kind": "deleted"} for path in before if path not in after)
    return changes
