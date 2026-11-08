"""Live smoke tests through the wrappers. Skipped unless STRATA_LIVE=1.

    STRATA_LIVE=1 .venv/bin/python -m pytest llm/tests/test_live.py
"""
import os

import pytest

from llm.main.muse import MuseClient
from llm.main.subq import SubqClient

pytestmark = pytest.mark.skipif(os.environ.get("STRATA_LIVE") != "1",
                                reason="set STRATA_LIVE=1 to call the real APIs")


def test_live_muse_and_budget():
    client = MuseClient()
    result = client.complete("Reply with exactly one word: pong")
    assert "pong" in result.text.lower()
    assert client.budget.summary()["muse"]["used"] > 0


def test_live_subq():
    client = SubqClient()
    p = client.noul({"claim": "Defuse always works", "trace": "seat 1 had Defuse and died"},
                    "Does the trace contradict the claim?")
    assert p > 0.5
    assert client.budget.summary()["subq"]["used"] > 0
