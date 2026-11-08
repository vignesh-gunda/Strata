"""Read-only file access jailed to `dig-site/`.

This is the only way agents read files. It refuses:
  * absolute paths, `..` segments, NUL bytes
  * symlinks that resolve outside the root
  * everything under `bin/` (the binary is run, never read)
Anything outside `dig-site/` (notably `services/`) is unreachable by construction.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

DENIED_TOP_LEVEL = frozenset({"bin"})
MAX_READ_BYTES = 8 * 1024 * 1024


class AccessDenied(PermissionError):
    """Raised for any path the agent is not allowed to open."""


class DigSiteFS:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"dig-site root not found: {self.root.name}")

    # ----- path policy -----
    def _resolve(self, rel: str | Path) -> Path:
        text = str(rel)
        if not text or "\x00" in text or os.path.isabs(text):
            raise AccessDenied("path must be relative to dig-site")
        normalized = os.path.normpath(text)
        if normalized == ".." or normalized.startswith(".." + os.sep):
            raise AccessDenied("path escapes dig-site")
        self._deny_if_denied(Path(normalized))
        resolved = (self.root / normalized).resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            raise AccessDenied("path escapes dig-site") from None
        self._deny_if_denied(relative)  # catches symlinks that point into bin/
        return resolved

    @staticmethod
    def _deny_if_denied(relative: Path) -> None:
        # Compare lowercased: macOS volumes are usually case-insensitive.
        if relative.parts and relative.parts[0].lower() in DENIED_TOP_LEVEL:
            raise AccessDenied("bin/ cannot be read; run the binary instead")

    # ----- read API -----
    def read_text(self, rel: str | Path, max_bytes: int = MAX_READ_BYTES) -> str:
        path = self._resolve(rel)
        if path.stat().st_size > max_bytes:
            raise AccessDenied(f"file larger than {max_bytes} bytes")
        return path.read_text(encoding="utf-8")

    def iter_jsonl(self, rel: str | Path) -> Iterator[dict[str, Any]]:
        path = self._resolve(rel)
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def exists(self, rel: str | Path) -> bool:
        try:
            return self._resolve(rel).exists()
        except AccessDenied:
            return False

    def listdir(self, rel: str | Path = ".") -> list[str]:
        path = self._resolve(rel) if str(rel) not in (".", "") else self.root
        names = sorted(p.name for p in path.iterdir())
        if path == self.root:
            names = [n for n in names if n.lower() not in DENIED_TOP_LEVEL]
        return names

    def manifest(self) -> dict[str, Any]:
        return json.loads(self.read_text("MANIFEST.json"))

    # ----- internal: used only by sandbox.main.binary / metadata -----
    def _binary_path(self) -> Path:
        path = (self.root / "bin" / "pressure.pyz").resolve()
        path.relative_to(self.root)  # must still live inside the jail
        if not path.is_file():
            raise FileNotFoundError("binary not built; run the dig-site build first")
        return path
