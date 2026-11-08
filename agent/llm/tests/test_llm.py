"""Offline tests for the llm component. A fake transport replaces the network."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from llm.main import firewall
from llm.main.budget import Budget, BudgetExceeded
from llm.main.env import load_env, require_key
from llm.main.http import post_with_retry
from llm.main.muse import MuseClient, MuseError
from llm.main.subq import SubqClient, SubqError


class FakeTransport:
    """Replays queued (status, payload) responses and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": body})
        return self.responses.pop(0)


def muse_ok(text="pong", total=10):
    return 200, {
        "output": [{"type": "reasoning"},
                   {"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 4, "output_tokens": total - 4, "total_tokens": total},
    }


def no_sleep(_):
    pass


# ---------- env ----------

def test_load_env_does_not_override_and_require_key_hides_value(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nFOO_KEY='abc'\nBAR_KEY=zzz\n")
    monkeypatch.setenv("BAR_KEY", "existing")
    monkeypatch.delenv("FOO_KEY", raising=False)
    load_env(env)
    assert require_key("FOO_KEY") == "abc"
    assert require_key("BAR_KEY") == "existing"
    monkeypatch.delenv("MISSING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MISSING_KEY is not set"):
        require_key("MISSING_KEY")


# ---------- firewall ----------

@pytest.mark.parametrize("text", [
    "see services/pressure/blackbox/rules.py",
    "open sealed/RULES.md",
    "DIG_SITE_KEY says",
    r"services\pressure",
])
def test_firewall_blocks_forbidden(text):
    with pytest.raises(firewall.PromptFirewallError):
        firewall.check(text)


def test_firewall_blocks_inside_structured_state_and_allows_dig_site():
    with pytest.raises(firewall.PromptFirewallError):
        firewall.check({"note": ["x", "services/pressure"]})
    firewall.check("dig-site/source/engine.py defines _scan", {"trace": "t-000001"})


# ---------- budget ----------

def test_budget_blocks_after_cap_and_counts():
    budget = Budget(caps={"muse": 15})
    budget.ensure("muse")
    budget.record("muse", 10)
    budget.ensure("muse")
    budget.record("muse", 10)
    with pytest.raises(BudgetExceeded):
        budget.ensure("muse")
    assert budget.summary()["muse"] == {"used": 20, "calls": 2, "cap": 15}


# ---------- http retry ----------

def test_retries_429_and_5xx_then_succeeds_and_backs_off():
    sleeps = []
    t = FakeTransport((429, {}), (503, {}), (200, {"ok": 1}))
    status, payload = post_with_retry("u", "k", {}, transport=t, sleep=sleeps.append)
    assert (status, payload) == (200, {"ok": 1})
    assert sleeps == [1.0, 2.0]


def test_does_not_retry_400():
    t = FakeTransport((400, {"x": 1}), (200, {}))
    status, _ = post_with_retry("u", "k", {}, transport=t, sleep=no_sleep)
    assert status == 400 and len(t.requests) == 1


def test_gives_up_after_retries():
    t = FakeTransport(*[(500, {})] * 4)
    status, _ = post_with_retry("u", "k", {}, transport=t, sleep=no_sleep, retries=3)
    assert status == 500 and len(t.requests) == 4


# ---------- Muse ----------

def test_muse_complete_parses_text_sends_bearer_and_counts_budget():
    t = FakeTransport(muse_ok("pong", total=12))
    client = MuseClient("secret-key", transport=t, sleep=no_sleep)
    result = client.complete("ping", system="be brief")
    assert result.text == "pong" and result.model == "muse-spark-1.3"
    request = t.requests[0]
    assert request["url"].endswith("/responses")
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    assert request["body"]["instructions"] == "be brief"
    assert client.budget.summary()["muse"]["used"] == 12


def test_muse_falls_back_to_older_model_on_404():
    t = FakeTransport((404, {"error": {"message": "model_not_found"}}), muse_ok())
    client = MuseClient("k", transport=t, sleep=no_sleep)
    assert client.complete("hi").model == "muse-spark-1.1"
    assert client.complete.__self__._model_index == 1  # sticks to the fallback


def test_muse_error_has_status_and_no_key_in_message():
    t = FakeTransport((401, {"error": {"message": "invalid_api_key"}}))
    client = MuseClient("secret-key", transport=t, sleep=no_sleep)
    with pytest.raises(MuseError) as info:
        client.complete("hi")
    assert info.value.status == 401 and "secret-key" not in str(info.value)


def test_muse_blocks_forbidden_prompt_before_any_request():
    t = FakeTransport()
    client = MuseClient("k", transport=t)
    with pytest.raises(firewall.PromptFirewallError):
        client.complete("read services/pressure/blackbox/rules.py")
    assert t.requests == []


def test_muse_stops_when_budget_used_up():
    client = MuseClient("k", budget=Budget(caps={"muse": 5}),
                        transport=FakeTransport(muse_ok(total=10)), sleep=no_sleep)
    client.complete("one")
    with pytest.raises(BudgetExceeded):
        client.complete("two")


class Rule(BaseModel):
    name: str
    delta: int


def test_muse_complete_json_accepts_fenced_json():
    t = FakeTransport(muse_ok('```json\n{"name": "vent", "delta": -4}\n```'))
    rule = MuseClient("k", transport=t, sleep=no_sleep).complete_json("rule?", Rule)
    assert rule == Rule(name="vent", delta=-4)


def test_muse_complete_json_retries_once_then_fails():
    t = FakeTransport(muse_ok("not json"), muse_ok('{"name": "x"}'))
    client = MuseClient("k", transport=t, sleep=no_sleep)
    with pytest.raises(MuseError, match="valid JSON"):
        client.complete_json("rule?", Rule)
    assert len(t.requests) == 2
    assert "previous answer was invalid" in t.requests[1]["body"]["input"]


def test_muse_complete_json_recovers_on_second_try():
    t = FakeTransport(muse_ok("oops"), muse_ok('{"name": "gear1", "delta": 1}'))
    rule = MuseClient("k", transport=t, sleep=no_sleep).complete_json("rule?", Rule)
    assert rule.delta == 1


# ---------- SubQ ----------

SUBQ_OK = (200, {
    "answers": {
        "q": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1},
              "confidence": 0.8},
    },
    "usage": {"input_tokens": 50, "output_tokens": 0},
})


def test_subq_choice_returns_typed_result_and_counts_tokens():
    t = FakeTransport(SUBQ_OK)
    client = SubqClient("k", transport=t, sleep=no_sleep)
    result = client.choice({"claim": "x"}, "pick", {"a": None, "b": "second"})
    assert result.choice == "a" and result.confidence == 0.8
    assert t.requests[0]["url"].endswith("/systemone")
    assert t.requests[0]["body"]["model"] == "subq-snap-1.1"
    assert client.budget.summary()["subq"]["used"] == 50


def test_subq_noul_and_score():
    noul = SubqClient("k", transport=FakeTransport(
        (200, {"answers": {"q": {"type": "noul", "noul": 0.97}}, "usage": {"input_tokens": 1}})))
    assert noul.noul("s", "contradicted?") == pytest.approx(0.97)
    score = SubqClient("k", transport=FakeTransport(
        (200, {"answers": {"q": {"type": "score", "score": 1.5, "legend": {"0": "lo", "1": "hi"},
                                 "probabilities": {"0": 0.5, "1": 0.5}, "confidence": 0.6}},
               "usage": {"input_tokens": 1}})))
    assert score.score("s", "how?", ["lo", "hi"]).score == 1.5


@pytest.mark.parametrize("status,kind", [(401, "unauthorized"), (402, "payment_required"),
                                         (422, "validation_error")])
def test_subq_fails_loudly_without_retrying(status, kind):
    t = FakeTransport((status, {"detail": {"error_type": kind, "message": "nope"}}), SUBQ_OK)
    with pytest.raises(SubqError) as info:
        SubqClient("k", transport=t, sleep=no_sleep).noul("s", "q")
    assert info.value.status == status and info.value.error_type == kind
    assert len(t.requests) == 1


def test_subq_retries_503():
    t = FakeTransport((503, {}), SUBQ_OK)
    SubqClient("k", transport=t, sleep=no_sleep).choice("s", "q", {"a": None, "b": None})
    assert len(t.requests) == 2


def test_subq_blocks_forbidden_state():
    t = FakeTransport()
    with pytest.raises(firewall.PromptFirewallError):
        SubqClient("k", transport=t).noul({"file": "services/pressure/x.py"}, "q")
    assert t.requests == []
