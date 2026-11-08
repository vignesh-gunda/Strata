"""Token budget guard shared by the Muse and SubQ clients.

Caps are in tokens because that is what both APIs report. Convert the Muse
dollar budget into a token cap with your own price when you set it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised before a call when the model's token cap is already used up."""


@dataclass
class Budget:
    caps: dict[str, int] = field(
        default_factory=lambda: {"muse": 50_000_000, "subq": 100_000_000}
    )
    used: dict[str, int] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def ensure(self, model: str) -> None:
        """Call before sending a request."""
        with self._lock:
            cap = self.caps.get(model)
            if cap is not None and self.used.get(model, 0) >= cap:
                raise BudgetExceeded(f"{model} token budget of {cap} is used up")

    def record(self, model: str, tokens: int) -> None:
        """Call after a successful response."""
        with self._lock:
            self.used[model] = self.used.get(model, 0) + max(0, int(tokens or 0))
            self.calls[model] = self.calls.get(model, 0) + 1

    def summary(self) -> dict[str, dict[str, int | None]]:
        with self._lock:
            return {
                m: {"used": self.used.get(m, 0), "calls": self.calls.get(m, 0),
                    "cap": self.caps.get(m)}
                for m in set(self.caps) | set(self.used)
            }
