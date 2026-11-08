"""Binary metadata only: size, hash, entry point. Never the contents."""
from __future__ import annotations

import hashlib
from typing import Any

from .digsite_fs import DigSiteFS


def binary_metadata(fs: DigSiteFS) -> dict[str, Any]:
    path = fs._binary_path()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)

    entry: dict[str, Any] = {}
    for artifact in fs.manifest().get("artifacts", []):
        if artifact.get("kind") == "binary":
            entry = artifact
            break
    listed = (entry.get("files") or [{}])[0]
    return {
        "path": "bin/pressure.pyz",
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "manifest_sha256": listed.get("sha256"),
        "matches_manifest": listed.get("sha256") == digest.hexdigest(),
        "entry_point": "JSON lines on stdin/stdout; start the process, send requests",
        "run_hint": entry.get("description"),
    }
