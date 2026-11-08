"""Child process that runs ONE model-written rule. Started by `rule_worker.RuleWorker`.

    python -I _rule_worker_main.py <cpu_seconds>

Hardening (best effort, NOT a security boundary against a determined attacker):
  * isolated interpreter (-I), empty environment: no API keys to read
  * CPU time limit via setrlimit; the parent also enforces a wall-clock timeout
  * imports are limited to a small allow-list of pure modules
  * open / exec / eval / compile / input are removed from the rule's builtins
  * stdout is redirected, so a rule's print() cannot corrupt the protocol

A rule module defines:
    KIND   = "events" | "legal" | "view" | "setup"
    COVERS = [...event types...]            (events rules)
    def predict(...)  -> prediction or None   (None = "this step is not mine")
"""
import builtins
import copy
import io
import json
import os
import resource
import sys

ALLOWED = {"json", "math", "collections", "itertools", "functools", "copy", "re", "typing",
           "dataclasses", "enum", "operator", "heapq", "bisect", "string", "fractions",
           "__future__"}
KINDS = {"events", "legal", "view", "setup"}


def guarded_import(real_import):
    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name.split(".")[0] not in ALLOWED:
            raise ImportError(f"import of {name!r} is not allowed in a rule")
        return real_import(name, globals, locals, fromlist, level)
    return _import


def restricted_builtins(real_import):
    safe = dict(vars(builtins))
    for name in ("open", "exec", "eval", "compile", "input", "breakpoint", "exit", "quit",
                 "help", "memoryview"):
        safe.pop(name, None)
    safe["__import__"] = guarded_import(real_import)
    safe["print"] = lambda *a, **k: None
    return safe


class Rule:
    def __init__(self):
        self.kind = None
        self.covers = []
        self.predict = None
        self.live_history = []

    def load(self, code, real_import):
        namespace = {"__builtins__": restricted_builtins(real_import), "__name__": "rule"}
        exec(builtins.compile(code, "<rule>", "exec"), namespace)
        kind = namespace.get("KIND")
        if kind not in KINDS:
            raise ValueError(f"rule must define KIND in {sorted(KINDS)}")
        predict = namespace.get("predict")
        if not callable(predict):
            raise ValueError("rule must define predict(...)")
        covers = list(namespace.get("COVERS", []))
        if kind == "events" and not covers:
            raise ValueError("an events rule must define a non-empty COVERS list")
        self.kind, self.covers, self.predict = kind, covers, predict
        self.live_history = []
        return {"kind": kind, "covers": covers}

    def call(self, history, step):
        """Run predict() for one step. `step` carries before/action/events/legal."""
        k = self.kind
        if k == "events":
            prior = [e for e in step["events"] if e["type"] not in self.covers] \
                if step.get("events") is not None else []
            return self.predict(history, copy.deepcopy(step["before"]),
                                copy.deepcopy(step["action"]), copy.deepcopy(prior))
        if k == "legal":
            return self.predict(history, copy.deepcopy(step["before"]))
        if k == "view":
            return self.predict(history, copy.deepcopy(step["before"]),
                                copy.deepcopy(step["action"]), copy.deepcopy(step["events"]))
        raise ValueError("setup rules are evaluated per game")


def entry(step):
    return {"seat": step.get("seat"), "turn": step.get("turn"), "action": step.get("action"),
            "legal": step.get("legal"), "events": step.get("events"), "view": step.get("view"),
            "before": step.get("before")}


def main():
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[1]), int(sys.argv[1])))
    except (ValueError, OSError, IndexError):
        pass
    proto = sys.stdout
    sys.stdout = open(os.devnull, "w")
    real_import = builtins.__import__
    rule = Rule()
    builtins.__import__ = guarded_import(real_import)

    def reply(request_id, ok, payload):
        key = "result" if ok else "error"
        proto.write(json.dumps({"id": request_id, "ok": ok, key: payload}) + "\n")
        proto.flush()

    for line in sys.stdin:
        request = json.loads(line)
        rid, method, params = request["id"], request["method"], request.get("params", {})
        try:
            if method == "load":
                reply(rid, True, rule.load(params["code"], real_import))
            elif method == "evaluate_game":
                history, out = [], []
                for step in params["steps"]:
                    try:
                        out.append({"p": rule.call(history, step)})
                    except Exception as error:   # a crashing rule is reported, not fatal
                        out.append({"err": f"{type(error).__name__}: {error}"[:300]})
                    history.append(entry(step))
                reply(rid, True, out)
            elif method == "evaluate_setup":
                out = []
                for game in params["games"]:
                    try:
                        out.append({"p": rule.predict(game["players"], list(game["deck"]))})
                    except Exception as error:
                        out.append({"err": f"{type(error).__name__}: {error}"[:300]})
                reply(rid, True, out)
            elif method == "live_reset":
                rule.live_history = []
                reply(rid, True, None)
            elif method == "live_predict":
                try:
                    reply(rid, True, {"p": rule.call(rule.live_history, params["step"])})
                except Exception as error:
                    reply(rid, True, {"err": f"{type(error).__name__}: {error}"[:300]})
            elif method == "live_observe":
                rule.live_history.append(entry(params["step"]))
                reply(rid, True, None)
            else:
                reply(rid, False, {"kind": "invalid_request", "message": f"unknown method {method}"})
        except Exception as error:               # load errors, bad params
            reply(rid, False, {"kind": "rule_error",
                               "message": f"{type(error).__name__}: {error}"[:300]})


if __name__ == "__main__":
    main()
