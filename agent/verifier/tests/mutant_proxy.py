"""Wraps the real binary and plants one bug: Vent's pressure is off by one.

    python mutant_proxy.py <path to pressure.pyz> [--identity]
"""
import json
import subprocess
import sys

IDENTITY = "--identity" in sys.argv
child = subprocess.Popen([sys.executable, sys.argv[1]], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, bufsize=1)
for line in sys.stdin:
    request = json.loads(line)
    child.stdin.write(line)
    child.stdin.flush()
    reply = json.loads(child.stdout.readline())
    if (not IDENTITY and request["method"] == "act" and reply.get("ok")
            and request["params"]["action"].get("card") == "vent"):
        for event in reply["result"]["events"]:
            if event["type"] == "pressure":
                event["value"] += 1
                reply["result"]["view"]["pressure"] += 1
                break
    print(json.dumps(reply), flush=True)
