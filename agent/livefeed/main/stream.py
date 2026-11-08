"""T15.3: push the feed to the people watching.

  * `FeedWatcher`  tails `events.jsonl` and hands each new line to a callback. The
                   coordinator uses it to post lines into the local chat as each loop ends.
  * `follow()`     a generator for any reader that wants to block (the UI server).
  * `main()`       `python -m livefeed.main.stream --run-id r-01 --follow` tails it in a terminal.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from .events import Event, read_events
from .lines import line
from .next_step import next_line

AGENT_ROOT = Path(__file__).resolve().parents[2]


def lines_for(history: list[Event], event: Event) -> list[str]:
    """The action line, then its "Next:" line when there is one. `history` ends at `event`."""
    out = [line(event)]
    upcoming = next_line(history)
    if upcoming:
        out.append("         " + upcoming)
    return out


def replay_events(run_dir: Path, speed: float = 10.0, *, max_gap: float = 2.0,
                  sleep: Callable[[float], None] = time.sleep) -> Iterator[tuple[Event, list[Event]]]:
    """Re-send a saved run in order, pacing it by the recorded timestamps divided by `speed`.

    Yields (event, history up to and including it). `speed <= 0` means no waiting at all.
    A long recorded pause (a slow model call) is capped at `max_gap` seconds before scaling.
    """
    from datetime import datetime
    events = read_events(run_dir)
    previous = None
    for index, event in enumerate(events):
        if speed > 0 and previous is not None:
            try:
                gap = (datetime.fromisoformat(event.at) - datetime.fromisoformat(previous.at)).total_seconds()
            except ValueError:
                gap = 0.0
            sleep(min(max(gap, 0.0), max_gap) / speed)
        previous = event
        yield event, events[: index + 1]


def follow(run_dir: Path, after: int = 0, *, poll: float = 0.4,
           stop: Callable[[], bool] = lambda: False,
           idle: Callable[[], None] | None = None) -> Iterator[Event]:
    """Yield events as they are written. Calls `idle()` on each empty poll; ends when `stop()`."""
    last = after
    while True:
        fresh = read_events(run_dir, last)
        for event in fresh:
            last = event.loop
            yield event
        if stop():
            return
        if not fresh:
            if idle:
                idle()
            time.sleep(poll)


class FeedWatcher:
    """Calls `emit(line)` once per new event, in order, from a background thread."""

    def __init__(self, run_dir: Path, emit: Callable[[str], None], *, poll: float = 0.3,
                 from_start: bool = False):
        self.run_dir = Path(run_dir)
        self._emit = emit
        self._poll = poll
        self._lock = threading.Lock()
        self._last = 0 if from_start else self._current_loop()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _current_loop(self) -> int:
        events = read_events(self.run_dir)
        return events[-1].loop if events else 0

    def drain(self) -> int:
        """Emit everything new right now. Safe to call from any thread."""
        sent = 0
        with self._lock:
            fresh = read_events(self.run_dir, self._last)
            if fresh:
                everything = read_events(self.run_dir)
            for event in fresh:
                self._last = event.loop
                for text in lines_for([e for e in everything if e.loop <= event.loop], event):
                    self._emit(text)
                sent += 1
        return sent

    def start(self) -> "FeedWatcher":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="feed-watcher", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.drain()
            except Exception:                  # a bad read must not kill the feed
                continue

    def stop(self) -> None:
        """Stop the thread, then send anything that landed after the last poll."""
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self.drain()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tail the agent's live action feed.")
    parser.add_argument("--run-id", default="r-01")
    parser.add_argument("--runs-dir", default=str(AGENT_ROOT / "runs"))
    parser.add_argument("--follow", "-f", action="store_true", help="keep waiting for new lines")
    parser.add_argument("--last", type=int, default=20, help="show only the last N existing lines (0 = all)")
    parser.add_argument("--replay", type=float, default=None, metavar="SPEED",
                        help="replay the saved run in order at SPEED times real time (0 = instantly)")
    args = parser.parse_args(argv)
    if args.replay is not None:
        run = Path(args.runs_dir) / args.run_id
        count = 0
        try:
            for event, history in replay_events(run, args.replay):
                count += 1
                for text in lines_for(history, event):
                    print(text, flush=True)
        except KeyboardInterrupt:
            return 0
        if not count:
            print(f"No events to replay for run {args.run_id}.", file=sys.stderr)
        return 0

    run_dir = Path(args.runs_dir) / args.run_id
    existing = read_events(run_dir)
    shown = existing[-args.last:] if args.last else existing
    for event in shown:
        for text in lines_for([e for e in existing if e.loop <= event.loop], event):
            print(text, flush=True)
    if not args.follow:
        if not existing:
            print(f"No events yet for run {args.run_id}.", file=sys.stderr)
        return 0
    try:
        for event in follow(run_dir, existing[-1].loop if existing else 0):
            for text in lines_for(read_events(run_dir, 0)[: event.loop], event):
                print(text, flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
