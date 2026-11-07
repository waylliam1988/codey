"""Read-only request admission view, independent of a concrete provider."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from codey.types.json_value import JsonObject


class AttemptGateClient(Protocol):
    @property
    def purpose(self) -> Literal["conversation", "compaction"]: ...

    @property
    def _attempt_gate_prompt(self) -> str: ...


@dataclass(frozen=True)
class AttemptGateView:
    purpose: Literal["conversation", "compaction"]
    _attempt_gate_prompt: str


AttemptGateCallback: TypeAlias = Callable[[AttemptGateClient, JsonObject], None]
