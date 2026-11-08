"""What a rule is, and the groups of behavior the agent must explain.

A group is one thing to explain (for example the `pressure` events). Competing
rules in a group are judged against each other by the Scorer. Together the
confirmed rules of all groups describe the whole program.
"""
from __future__ import annotations

from dataclasses import dataclass

CONTRACT = '''\
A rule is a small Python module (standard library only: json, math, collections, itertools,
functools, copy, re, typing, dataclasses, enum, operator, heapq, bisect; no files, no network).
It predicts what the real program did, and is judged only against recorded steps.

Data shapes (all plain JSON):
  action  = {"type": "play", "card": NAME} | {"type": "draw"} | {"type": "pass"}
  view    = {"players": int, "hands": [[card,...] per seat], "alive": [bool per seat],
             "discard": [card,...], "draw_pile_size": int, "current_seat": int,
             "actions_left": int, "pressure": int, "turn": int, "over": bool, "winner": int|null}
            (the draw pile ORDER, direction, jams and other hidden state are NOT in the view)
  events (in the order the program emits them within one step):
    {"type":"played","seat":s,"card":c}          {"type":"pressure","value":v}
    {"type":"eliminated","seat":s}               {"type":"revealed","seat":s,"cards":[c,...]}
    {"type":"drew","seat":s,"card":c}            {"type":"passed","seat":s}
    {"type":"turn_passed","to":s,"actions":n,"turn":t}   {"type":"game_over","winner":s|null}
  history = list of the PREVIOUS steps of this game, oldest first. Each entry is
            {"seat","turn","action","legal","events","view" (state AFTER that step),"before"}.
            Use it to track hidden state (for example whether a seat is jammed).

Every rule defines KIND and predict():

KIND = "events"   -- predicts the events of the types listed in COVERS
    COVERS = ["pressure"]
    def predict(history, view, action, prior_events):
        # view = state BEFORE the action. prior_events = the actual events of this step whose
        # types are NOT in COVERS (it may be empty: do not depend on it more than needed).
        # Return the list of COVERED events this step emits, in order ([] if none),
        # or None if this rule has nothing to say about this step.
KIND = "legal"    -- predicts the list of legal actions before an action
    def predict(history, view): return [action, ...]   (order does not matter) or None
KIND = "view"     -- predicts the whole view AFTER an action, given what actually happened
    def predict(history, view, action, events): return full_view_dict or None
KIND = "setup"    -- predicts the initial view of a new game from the players and the deck
    def predict(players, deck): return full_view_dict
    (deck = list of card names; hands are dealt round-robin from the top of the deck)

Rules of the game for rule writers:
  * A rule that returns a prediction must be EXACTLY right (equal to what was recorded).
    Return None for steps you do not understand instead of guessing.
  * Judge only from the data you were shown; do not rely on outside knowledge of any card game.
  * Never mutate your inputs.
'''


@dataclass(frozen=True)
class Group:
    name: str
    kind: str
    covers: tuple[str, ...]
    doc: str


GROUPS: dict[str, Group] = {g.name: g for g in [
    Group("setup", "setup", (), "The initial view of a new game: hands dealt from the deck, "
          "pile size, pressure, whose turn it is, actions left, turn number."),
    Group("legal", "legal", (), "Which actions are legal before each action: plays, draw, pass."),
    Group("played", "events", ("played",), "The `played` event: when it is emitted for a play."),
    Group("pressure", "events", ("pressure",), "The `pressure` events: the gauge value after a "
          "card's effect, including what happens at overflow and when a card has no effect."),
    Group("eliminated", "events", ("eliminated",), "The `eliminated` event: when a player is out."),
    Group("revealed", "events", ("revealed",), "The `revealed` event: what a scan shows."),
    Group("drew", "events", ("drew",), "The `drew` event: which card a draw takes."),
    Group("passed", "events", ("passed",), "The `passed` event."),
    Group("turn_passed", "events", ("turn_passed",), "The `turn_passed` event: who plays next, "
          "how many actions they get, and the turn counter. This is the whole turn-flow logic."),
    Group("game_over", "events", ("game_over",), "The `game_over` event: when the game ends and "
          "who wins."),
    Group("view", "view", (), "The full view after an action, given the actual events: hands, "
          "alive, discard, draw_pile_size, current_seat, actions_left, pressure, turn, over, winner."),
]}

GROUP_ORDER = ["setup", "legal", "played", "pressure", "eliminated", "revealed", "drew", "passed",
               "turn_passed", "game_over", "view"]
