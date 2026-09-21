"""FaultController: deterministic fault injection that never touches business state.

The controller only decides timing and destruction: timeout, kill, delay,
duplicate, disconnect, crash, race. It never writes logs, effects, ghost
events, or approvals -- those belong to the world under test. Every random
choice flows from a seeded ``random.Random`` so a failing seed replays the
exact same failure.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

TIMEOUT = "timeout"
KILL = "kill"
DELAY = "delay"
DUPLICATE = "duplicate"
DISCONNECT = "disconnect"
CRASH = "crash"
RACE = "race"

ALL_FAULTS = (TIMEOUT, KILL, DELAY, DUPLICATE, DISCONNECT, CRASH, RACE)


@dataclass
class FaultSpec:
    kind: str
    target: str = ""
    detail: str = ""


class FaultController:
    def __init__(self, seed: int = 0) -> None:
        self.seed = int(seed)
        self.rng = random.Random(int(seed))
        self.injected: list[FaultSpec] = []

    def reseed(self, seed: int) -> None:
        self.seed = int(seed)
        self.rng = random.Random(int(seed))
        self.injected.clear()

    def _record(self, kind: str, target: str = "", detail: str = "") -> FaultSpec:
        spec = FaultSpec(kind=kind, target=target, detail=detail)
        self.injected.append(spec)
        return spec

    def should_timeout(self, target: str = "", *, probability: float = 0.5) -> bool:
        """Whether the next send should raise TimeoutError instead of replying."""
        if self.rng.random() < probability:
            self._record(TIMEOUT, target)
            return True
        return False

    def should_kill(self, target: str = "", *, probability: float = 0.5) -> bool:
        """Whether the world should be restarted (handles dropped, stores reopened)."""
        if self.rng.random() < probability:
            self._record(KILL, target)
            return True
        return False

    def duplicate_count(self, target: str = "", *, maximum: int = 3) -> int:
        """How many extra copies of a delivery to inject (0 means none)."""
        extra = self.rng.randint(0, maximum)
        if extra:
            self._record(DUPLICATE, target, detail=f"x{extra}")
        return extra

    def should_disconnect(self, target: str = "", *, probability: float = 0.5) -> bool:
        if self.rng.random() < probability:
            self._record(DISCONNECT, target)
            return True
        return False

    def choose(self, target: str, options: tuple) -> object:
        """Deterministic choice that is also recorded for replay inspection."""
        if not options:
            raise ValueError("choose needs at least one option")
        picked = self.rng.choice(list(options))
        self._record(RACE, target, detail=str(picked))
        return picked

    def weighted(self, target: str, options: tuple[tuple[str, int], ...]) -> str:
        """Pick one name by weight; still just a decision, never business state."""
        if not options:
            raise ValueError("weighted needs at least one option")
        total = sum(weight for _, weight in options)
        if total <= 0:
            raise ValueError("weighted needs a positive total weight")
        roll = self.rng.uniform(0, total)
        for name, weight in options:
            roll -= weight
            if roll <= 0:
                self._record(RACE, target, detail=name)
                return name
        name = options[-1][0]
        self._record(RACE, target, detail=name)
        return name

    def coin_flip(self, target: str = "") -> bool:
        if self.rng.random() < 0.5:
            self._record(RACE, target, detail="heads")
            return True
        self._record(RACE, target, detail="tails")
        return False


__all__ = [
    "ALL_FAULTS",
    "CRASH",
    "DELAY",
    "DISCONNECT",
    "DUPLICATE",
    "KILL",
    "RACE",
    "TIMEOUT",
    "FaultController",
    "FaultSpec",
]
