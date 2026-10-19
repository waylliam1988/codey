"""Provider window actions shared across planning and agent request types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codey.durable.context import ContextSnapshot
from codey.runtime.core.protocol_window import ProtocolWindowRef


class WindowOpenReason(StrEnum):
    FIRST_OPEN = "first_open"
    USER_RESET = "user_reset"
    MODEL_CHANGE = "model_change"
    CONTEXT_ROLLOVER = "context_rollover"


@dataclass(frozen=True)
class ReuseWindow:
    ref: ProtocolWindowRef


@dataclass(frozen=True)
class OpenWindow:
    reason: WindowOpenReason
    seed: str = ""
    context: ContextSnapshot | None = None


WindowAction = ReuseWindow | OpenWindow


__all__ = ["OpenWindow", "ReuseWindow", "WindowAction", "WindowOpenReason"]
