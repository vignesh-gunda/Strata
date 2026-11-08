"""Session API: the four calls agents get, with validated inputs.

``new_game(players, script)``, ``legal_actions(game_id)``,
``act(game_id, action)`` and ``view(game_id)``. ``reveal()`` returns the
planted mutations and the opaque-token map, for the final report only.
"""

from __future__ import annotations

from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from blackbox.engine import legal_actions as engine_legal_actions
from blackbox.engine import new_state, step
from blackbox.rules import Mutation, pick_mutations
from blackbox.scripts import script_from_seed
from blackbox.state import GameState
from blackbox.view import Codec, opaque_codec, public_view

MAX_DECK = 200
MAX_GAMES = 100_000


class GameNotFoundError(KeyError):
    """No game with that id exists in this black box."""


class ScriptModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck: list[str] = Field(min_length=1, max_length=MAX_DECK)


class NewGameModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    players: int = Field(ge=2, le=4)
    script: ScriptModel


class ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=16)
    card: str | None = Field(default=None, min_length=1, max_length=16)


class BlackBox:
    def __init__(self, mutations: Iterable[Mutation] = (), opaque_seed: int | None = None) -> None:
        self._mutations = tuple(mutations)
        self._codec = Codec() if opaque_seed is None else opaque_codec(opaque_seed)
        self._games: dict[str, GameState] = {}

    @classmethod
    def from_seed(cls, seed: int, mutation_count: int, opaque: bool = False) -> "BlackBox":
        return cls(pick_mutations(seed, mutation_count), opaque_seed=seed if opaque else None)

    def new_game(self, players: int, script: dict) -> dict:
        request = NewGameModel.model_validate({"players": players, "script": script})
        deck = [self._codec.decode_card(name) for name in request.script.deck]
        if len(self._games) >= MAX_GAMES:
            raise ValueError(f"game limit reached ({MAX_GAMES}); start a new black box")
        state = new_state(request.players, deck, self._mutations)
        game_id = f"g-{len(self._games) + 1:04d}"
        self._games[game_id] = state
        return {"game_id": game_id, "view": public_view(state, self._codec)}

    def random_script(self, seed: int) -> dict:
        """A shuffled standard deck written in the names agents see (works in opaque mode)."""
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError("seed must be an integer")
        return {"deck": [self._codec.encode_card(name) for name in script_from_seed(seed)["deck"]]}

    def legal_actions(self, game_id: str) -> list[dict]:
        encoded = [self._codec.encode_action(action) for action in engine_legal_actions(self._get(game_id))]
        return sorted(encoded, key=lambda action: (action["type"], action.get("card", "")))  # no catalogue-order leak

    def act(self, game_id: str, action: dict) -> dict:
        state = self._get(game_id)
        request = ActionModel.model_validate(action)
        after, events = step(state, self._codec.decode_action(request.type, request.card))
        self._games[game_id] = after
        return {
            "events": [self._codec.encode_event(event) for event in events],
            "view": public_view(after, self._codec),
        }

    def view(self, game_id: str) -> dict:
        return public_view(self._get(game_id), self._codec)

    def reveal(self) -> dict:
        return {"mutations": [mutation.value for mutation in self._mutations]} | self._codec.reveal()

    def _get(self, game_id: str) -> GameState:
        if game_id not in self._games:
            raise GameNotFoundError(game_id)
        return self._games[game_id]
