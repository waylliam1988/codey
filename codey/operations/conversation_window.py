"""Pure window actions and a small activation boundary for task operations."""

from __future__ import annotations

import inspect
from collections.abc import Mapping

from codey.durable.model import thaw_json
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.runtime.core.window_action import OpenWindow, ReuseWindow, WindowAction, WindowOpenReason


class WindowActivationError(RuntimeError):
    """The planned protocol window cannot be activated safely."""


def replayed_result_ids(action: WindowAction | None) -> frozenset[str]:
    """Identify results already seeded by a successful window activation."""
    if not isinstance(action, OpenWindow) or action.context is None:
        return frozenset()
    return frozenset(
        call_id for message in action.context.messages
        if message.get("role") == "tool"
        and isinstance(call_id := message.get("tool_call_id"), str) and call_id
    )


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
    # Window lifecycle is a declared provider capability. Do not infer it
    # from dynamic attributes on adapters or test doubles.
    # A concrete injected provider may declare its capability on the instance
    # (for example a headless test/CLI transport built with a closure).  Read
    # that explicit instance member before consulting the class, while still
    # refusing dynamic ``__getattr__`` fabrication from mocks/adapters.
    instance_attrs: dict[str, object]
    try:
        instance_attrs = vars(provider)
    except TypeError:
        instance_attrs = {}
    reset = instance_attrs.get("reset_window")
    if not callable(reset):
        reset = getattr(type(provider), "reset_window", None)
    if not callable(reset):
        reset = None
    if not callable(reset):
        declared_new_chat = instance_attrs.get("new_chat")
        if not callable(declared_new_chat):
            declared_new_chat = getattr(type(provider), "new_chat", None)
        if not callable(declared_new_chat):
            raise WindowActivationError("provider cannot open a protocol window")
        new_chat = object.__getattribute__(provider, "new_chat")
        try:
            new_chat()
            _restore_context(provider, action)
        except Exception as exc:
            raise WindowActivationError(f"provider new chat failed: {exc}") from exc
        current = getattr(provider, "window_ref", None)
        return current if isinstance(current, ProtocolWindowRef) else None
    try:
        opened = object.__getattribute__(provider, "reset_window")()
    except Exception as exc:
        raise WindowActivationError(f"protocol window reset failed: {exc}") from exc
    if not isinstance(opened, ProtocolWindowRef):
        raise WindowActivationError("provider returned an invalid protocol window reference")
    _restore_context(provider, action)
    return opened


def _restore_context(provider: object, action: OpenWindow) -> None:
    if action.context is None:
        return
    load = object.__getattribute__(provider, "load_durable_context")
    if not callable(load):
        raise WindowActivationError("provider cannot load planned durable context")
    try:
        messages = [thaw_json(message) for message in action.context.messages]
        parameters: Mapping[str, inspect.Parameter]
        try:
            parameters = inspect.signature(load).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "source_entry_ids" in parameters:
            load(messages, source_entry_ids=action.context.entry_ids)
        else:
            load(messages)
    except Exception as exc:
        raise WindowActivationError(f"durable context activation failed: {exc}") from exc


__all__ = ["OpenWindow", "ReuseWindow", "WindowAction", "WindowOpenReason", "WindowActivationError", "activate_window"]
