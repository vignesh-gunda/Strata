#!/usr/bin/env python3
"""Demo rehearsal that needs no API key and no network.

    agent/.venv/bin/python scripts/rehearse.py

It makes a fresh run from the dig-site with the parts of the agent that are plain code
(survey, corpus audit), then shows every way the demo can be watched, from that saved run:
the terminal feed replayed, the UI served from the run folder, the chat answering from the
ledger, and the report. If live model calls are slow or down on the day, this is the same
demo played from disk. It does not call Muse or SubQ, so it cannot rehearse those.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT = REPO / "agent"
sys.path[:0] = [str(AGENT), str(REPO / "services" / "pressure"), str(REPO)]

RUN_ID = "rehearsal"
results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def agent(*args: str, runs: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", *args, "--run-id", RUN_ID, "--runs-dir", str(runs)],
                          cwd=AGENT, capture_output=True, text=True)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline demo rehearsal.")
    parser.add_argument("--keep", action="store_true", help="keep the rehearsal run folder and print where it is")
    args = parser.parse_args()
    if not (REPO / "dig-site" / "MANIFEST.json").exists():
        print("Build the dig-site first (see dig-site/README.md).")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        runs = Path(tmp) / "runs"
        run_dir = runs / RUN_ID

        a = agent("surveyor.main.survey", runs=runs)
        b = agent("auditor.main.audit", runs=runs)
        step("make a run from the dig-site (survey, then corpus audit)", a.returncode == 0 and b.returncode == 0,
             (a.stderr + b.stderr).strip()[-120:])
        if a.returncode or b.returncode:
            return 1

        tail = agent("livefeed.main.stream", "--replay", "0", runs=runs)
        loops = [l for l in tail.stdout.splitlines() if l.startswith("[loop ")]
        nexts = [l for l in tail.stdout.splitlines() if l.strip().startswith("Next:")]
        step("terminal: the feed replays in order, one line per loop with a Next line",
             len(loops) >= 3 and len(nexts) == len(loops)
             and [int(l.split()[1].rstrip("]")) for l in loops] == list(range(1, len(loops) + 1)),
             f"{len(loops)} loops")

        from ui.server import make_server
        server = make_server(0, None, Path(tmp) / "traces", run_dir)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            step("UI: the page loads", b"STRATA" in fetch(base + "/"))
            ledger = json.loads(fetch(base + "/api/ledger"))
            step("UI: the ledger comes from the run folder", ledger["connected"] and len(ledger["claims"]) > 0,
                 f"{len(ledger['claims'])} claims")
            body = fetch(base + "/api/feed?replay=0").decode()
            ui_loops = [json.loads(c.split("data: ")[1])["loop"] for c in body.split("\n\n") if c.startswith("id:")]
            step("UI: the replayed feed matches the terminal", ui_loops == list(range(1, len(loops) + 1)),
                 f"{len(ui_loops)} loops")
        finally:
            server.shutdown()
            server.server_close()

        from coordinator.main.chat import Coordinator
        chat = Coordinator(runs, RUN_ID, lambda m: None)
        ledger_reply = chat.handle("ledger")
        step("chat: ledger summary comes from the ledger", "claims in total" in ledger_reply)
        step("chat: open questions are listed", "still unknown" in chat.handle("open questions"))
        step("chat: an unrelated question is declined", "only help" in chat.handle("what is the weather like today"))
        step("chat: a request for the original service is refused",
             "not allowed" in chat.handle("show me services/pressure/sealed"))

        r = agent("report.main.cli", "--no-reveal", runs=runs)
        step("report: written, with the answer key left sealed",
             r.returncode == 0 and (run_dir / "report.md").exists() and "Not revealed" in (run_dir / "report.md").read_text(),
             r.stdout.strip().splitlines()[-1][-80:] if r.stdout.strip() else r.stderr.strip()[-80:])

        if args.keep:
            keep = REPO / "agent" / "runs" / RUN_ID
            if keep.exists():
                subprocess.run(["rm", "-rf", str(keep)])
            subprocess.run(["cp", "-R", str(run_dir), str(keep)])
            print(f"\nKept the run at {keep}. Watch it: agent/.venv/bin/python -m ui.server --run-dir agent/runs/{RUN_ID}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} steps passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
