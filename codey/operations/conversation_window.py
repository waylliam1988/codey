"""Pure window actions and a small activation boundary for task operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

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


WindowAction = ReuseWindow | OpenWindow


class WindowActivationError(RuntimeError):
    """The planned protocol window cannot be activated safely."""


def activate_window(provider: object, action: WindowAction | None) -> ProtocolWindowRef | None:
    """Execute one planned action and return the provider's active reference."""
    if action is None:
        current = getattr(provider, "window_ref", None)
        return current if isinstance(current, ProtocolWindowRef) else None
    if isinstance(action, ReuseWindow):
        current = getattr(provider, "window_ref", None)
        if not isinstance(current, ProtocolWindowRef) or current != action.ref:
            raise WindowActivationError("protocol window reference mismatch")
        return current
    # Dynamic test doubles and legacy browser providers expose arbitrary
    # attributes through ``__getattr__``.  Only treat a reset implementation
    # declared by the provider type as the protocol-window API; otherwise the
    # browser lifecycle is the existing ``new_chat`` contract.
    reset = getattr(type(provider), "reset_window", None)
    if not callable(reset):
        reset = None
    if not callable(reset):
        new_chat = getattr(provider, "new_chat", None)
        if not callable(new_chat):
            raise WindowActivationError("provider cannot open a protocol window")
        try:
            new_chat()
        except Exception as exc:
            raise WindowActivationError(f"provider new chat failed: {exc}") from exc
        current = getattr(provider, "window_ref", None)
        return current if isinstance(current, ProtocolWindowRef) else None
    try:
        reset_provider: Any = provider
        opened = reset_provider.reset_window()
    except Exception as exc:
        raise WindowActivationError(f"protocol window reset failed: {exc}") from exc
    if not isinstance(opened, ProtocolWindowRef):
        raise WindowActivationError("provider returned an invalid protocol window reference")
    return opened


__all__ = ["OpenWindow", "ReuseWindow", "WindowAction", "WindowOpenReason", "WindowActivationError", "activate_window"]
