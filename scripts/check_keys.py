#!/usr/bin/env python3
"""Smoke-test the two model API keys Strata depends on.

Reads META_API_KEY and SUBQ_API_KEY from the repo-root .env (or the process
environment), makes one tiny call to each service, and prints PASS/FAIL.
Keys are never printed. Stdlib only, so it runs before any venv exists.

    python3 scripts/check_keys.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

META_BASE = os.environ.get("META_API_BASE", "https://api.meta.ai/v1")
META_MODELS = ["muse-spark-1.3", "muse-spark-1.1"]  # try newest first
SUBQ_BASE = os.environ.get("SUBQ_API_BASE", "https://api.subq.ai/v1")
SUBQ_MODEL = "subq-snap-1.1"


def load_env(path: Path) -> None:
    """Minimal .env loader. Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


def post_json(url: str, key: str, body: dict, timeout: int = 90) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except Exception:
            payload = {"raw": "<non-JSON error body>"}
        return error.code, payload


def check_muse(key: str) -> bool:
    for model in META_MODELS:
        started = time.time()
        status, payload = post_json(
            f"{META_BASE}/responses",
            key,
            {"model": model, "input": "Reply with exactly one word: pong"},
        )
        elapsed = time.time() - started
        if status == 200:
            text = ""
            for item in payload.get("output", []):
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        text += part.get("text", "")
            usage = payload.get("usage", {})
            print(f"  PASS  Muse Spark  model={model}  {elapsed:.1f}s  "
                  f"tokens={usage.get('total_tokens')}  reply={text.strip()!r}")
            return True
        err = payload.get("error", payload)
        print(f"  ....  Muse Spark  model={model}  HTTP {status}  {json.dumps(err)[:200]}")
        if status in (401, 403, 429):  # key problem: other model IDs won't help
            break
    print("  FAIL  Muse Spark")
    return False


def check_subq(key: str) -> bool:
    started = time.time()
    status, payload = post_json(
        f"{SUBQ_BASE}/systemone",
        key,
        {
            "model": SUBQ_MODEL,
            "state": {
                "claim": "Defuse always saves a player from an Exploding Kitten.",
                "trace": "t-001180: seat 1 held a Defuse on turn 3 and was eliminated.",
            },
            "questions": {
                "contradicted": {
                    "type": "noul",
                    "instructions": "Does the trace contradict the claim?",
                },
                "label": {
                    "type": "choice",
                    "instructions": "Classify the claim against the trace.",
                    "criteria": {
                        "supported": "The trace backs the claim",
                        "unsupported": "The trace does not back the claim",
                        "ambiguous": None,
                    },
                },
            },
        },
    )
    elapsed = time.time() - started
    if status == 200:
        answers = payload.get("answers", {})
        contradicted = answers.get("contradicted", {}).get("noul")
        label = answers.get("label", {}).get("choice")
        print(f"  PASS  SubQ Snap   model={SUBQ_MODEL}  {elapsed:.1f}s  "
              f"input_tokens={payload.get('usage', {}).get('input_tokens')}  "
              f"contradicted={contradicted}  label={label}")
        return True
    detail = payload.get("detail", payload)
    hint = {401: "bad key", 402: "no credits", 403: "key can't use this model"}.get(status, "")
    print(f"  FAIL  SubQ Snap   HTTP {status} {hint}  {json.dumps(detail)[:200]}")
    return False


def main() -> int:
    load_env(REPO_ROOT / ".env")
    results = {}
    for label, var, check in (
        ("Meta Muse Spark", "META_API_KEY", check_muse),
        ("SubQ Snap", "SUBQ_API_KEY", check_subq),
    ):
        key = os.environ.get(var, "")
        if not key:
            print(f"  FAIL  {label}: {var} is not set in .env or the environment")
            results[label] = False
            continue
        results[label] = check(key)
    ok = all(results.values())
    print("\nAll keys working." if ok else "\nSome checks failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
