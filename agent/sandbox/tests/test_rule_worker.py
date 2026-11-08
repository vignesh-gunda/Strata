"""T9 tests: model-written rule code runs in a limited child process."""
from __future__ import annotations

import pytest

from sandbox.main.rule_worker import RuleError, RuleTimeout, RuleWorker

GOOD = '''
KIND = "events"
COVERS = ["pressure"]
def predict(history, view, action, prior_events):
    if action.get("card") != "gear1":
        return None
    return [{"type": "pressure", "value": view["pressure"] + 1}]
'''


def steps():
    return [
        {"before": {"pressure": 2}, "action": {"type": "play", "card": "gear1"},
         "events": [{"type": "played", "seat": 0, "card": "gear1"}, {"type": "pressure", "value": 3}],
         "legal": [], "view": {"pressure": 3}, "seat": 0, "turn": 1},
        {"before": {"pressure": 3}, "action": {"type": "draw"}, "events": [], "legal": [],
         "view": {"pressure": 3}, "seat": 1, "turn": 2},
    ]


def test_load_reports_kind_and_covers_and_evaluates():
    with RuleWorker() as worker:
        assert worker.load(GOOD) == {"kind": "events", "covers": ["pressure"]}
        results = worker.evaluate_game(steps())
    assert results == [{"p": [{"type": "pressure", "value": 3}]}, {"p": None}]


def test_prior_events_exclude_the_rules_own_types():
    code = ('KIND = "events"\nCOVERS = ["pressure"]\n'
            'def predict(history, view, action, prior_events):\n'
            '    return [{"type": "pressure", "value": len(prior_events)}]\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_game(steps())
    assert out[0]["p"][0]["value"] == 1          # only 'played' is visible, not 'pressure'


def test_history_grows_with_each_step():
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    return [{"type": "pass"}] * len(history)\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_game(steps())
    assert [len(r["p"]) for r in out] == [0, 1]


def test_setup_rules_get_players_and_deck():
    code = ('KIND = "setup"\n'
            'def predict(players, deck):\n'
            '    return {"players": players, "first": deck[0]}\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_setup([{"players": 3, "deck": ["a", "b"]}])
    assert out == [{"p": {"players": 3, "first": "a"}}]


@pytest.mark.parametrize("code,message", [
    ("def predict(*a): return None", "KIND"),
    ('KIND = "bogus"\ndef predict(*a): return None', "KIND"),
    ('KIND = "legal"', "predict"),
    ('KIND = "events"\ndef predict(*a): return None', "COVERS"),
    ("this is not python(", "SyntaxError"),
    ('import os\nKIND = "legal"\ndef predict(h, v): return []', "not allowed"),
    ('import subprocess\nKIND = "legal"\ndef predict(h, v): return []', "not allowed"),
    ('import socket\nKIND = "legal"\ndef predict(h, v): return []', "not allowed"),
])
def test_bad_rules_are_rejected_at_load(code, message):
    with RuleWorker() as worker:
        with pytest.raises(RuleError, match=message):
            worker.load(code)


def test_rule_cannot_read_keys_or_files():
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    return open("/etc/passwd").read()\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_game(steps())
    assert all("err" in r and "NameError" in r["err"] for r in out)


def test_import_inside_function_is_also_blocked():
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    import os\n'
            '    return list(os.environ)\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_game(steps())
    assert all("ImportError" in r["err"] for r in out)


def test_environment_is_empty_of_secrets(monkeypatch):
    monkeypatch.setenv("META_API_KEY", "leak-me")
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    return [{"type": "x", "env": str(globals().get("META_API_KEY"))}]\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_game(steps())
    assert "leak-me" not in str(out)


def test_a_crashing_step_is_reported_not_fatal():
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    if len(history) == 0: raise ValueError("boom")\n'
            '    return []\n')
    with RuleWorker() as worker:
        worker.load(code)
        out = worker.evaluate_game(steps())
    assert "ValueError: boom" in out[0]["err"] and out[1] == {"p": []}


def test_print_does_not_corrupt_the_protocol():
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    print("hello")\n'
            '    return []\n')
    with RuleWorker() as worker:
        worker.load(code)
        assert worker.evaluate_game(steps()) == [{"p": []}, {"p": []}]


def test_infinite_loop_is_killed():
    code = 'KIND = "legal"\ndef predict(history, view):\n    while True: pass\n'
    worker = RuleWorker(timeout=0.5)
    worker.load(code)
    with pytest.raises(RuleTimeout):
        worker.evaluate_game(steps())
    worker.close()


def test_live_prediction_keeps_its_own_history():
    code = ('KIND = "legal"\n'
            'def predict(history, view):\n'
            '    return [{"type": "pass"}] * len(history)\n')
    with RuleWorker() as worker:
        worker.load(code)
        worker.live_reset()
        first = worker.live_predict(steps()[0])
        worker.live_observe(steps()[0])
        second = worker.live_predict(steps()[1])
    assert first == {"p": []} and second == {"p": [{"type": "pass"}]}
