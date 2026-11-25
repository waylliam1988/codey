"""Pure window actions and a small activation boundary for task operations."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Protocol

from codey.durable.model import thaw_json
from codey.providers.compat_transport import declared_callable
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.runtime.core.window_action import OpenWindow, ReuseWindow, WindowAction, WindowOpenReason
from codey.utils.object_fields import object_fields


class WindowActivationError(RuntimeError):
    """The planned protocol window cannot be activated safely."""


class _DurableActivation(Protocol):
    def __call__(self, messages: list[dict[str, object]], *, source_entry_ids: tuple[str, ...],
                 reason: str) -> object: ...


class _ContextLoaderWithSources(Protocol):
    def __call__(self, messages: list[dict[str, object]], *, source_entry_ids: tuple[str, ...]) -> object: ...


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
        return _current_window(provider)
    if isinstance(action, ReuseWindow):
        current = _current_window(provider)
        if current is None or current != action.ref:
            raise WindowActivationError("protocol window reference mismatch")
        return current
    if action.context is not None:
        activate: _DurableActivation | None = declared_callable(provider, "activate_durable_window")
        if activate is not None:
            try:
                opened = activate(_durable_messages(action), source_entry_ids=action.context.entry_ids, reason=action.reason.value)
            except Exception as exc:
                raise WindowActivationError(f"durable context activation failed: {exc}") from exc
            if not isinstance(opened, ProtocolWindowRef):
                raise WindowActivationError("provider returned an invalid protocol window reference")
            return opened
    restore = _prepare_context_restore(provider, action)
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
    reset: object = instance_attrs.get("reset_window")
    if not callable(reset):
        class_reset: object = getattr(type(provider), "reset_window", None)
        reset = class_reset
    if not callable(reset):
        declared_new_chat: object = instance_attrs.get("new_chat")
        if not callable(declared_new_chat):
            class_new_chat: object = getattr(type(provider), "new_chat", None)
            declared_new_chat = class_new_chat
        if not callable(declared_new_chat):
            raise WindowActivationError("provider cannot open a protocol window")
        try:
            new_chat: Callable[[], object] = object.__getattribute__(provider, "new_chat")
            new_chat()
            if restore is not None:
                restore()
        except Exception as exc:
            raise WindowActivationError(f"provider new chat failed: {exc}") from exc
        return _current_window(provider)
    try:
        reset_window: Callable[[], object] = object.__getattribute__(provider, "reset_window")
        opened = reset_window()
    except Exception as exc:
        raise WindowActivationError(f"protocol window reset failed: {exc}") from exc
    if not isinstance(opened, ProtocolWindowRef):
        raise WindowActivationError("provider returned an invalid protocol window reference")
    if restore is not None:
        restore()
    return opened


def _current_window(provider: object) -> ProtocolWindowRef | None:
    current: object = getattr(provider, "window_ref", None)
    return current if isinstance(current, ProtocolWindowRef) else None


def _prepare_context_restore(provider: object, action: OpenWindow) -> Callable[[], None] | None:
    if (context := action.context) is None:
        return None
    try:
        load: object = object.__getattribute__(provider, "load_durable_context")
    except AttributeError as exc:
        raise WindowActivationError("provider cannot load planned durable context") from exc
    if not callable(load):
        raise WindowActivationError("provider cannot load planned durable context")
    try:
        messages = _durable_messages(action)
    except Exception as exc:
        raise WindowActivationError(f"durable context activation failed: {exc}") from exc
    loader: Callable[[list[dict[str, object]]], object] = load
    try:
        parameters: Mapping[str, inspect.Parameter] = inspect.signature(loader).parameters
    except (TypeError, ValueError):
        parameters = {}

    def restore() -> None:
        try:
            if "source_entry_ids" in parameters:
                with_sources: _ContextLoaderWithSources = load
                with_sources(messages, source_entry_ids=context.entry_ids)
            else:
                loader(messages)
        except Exception as exc:
            raise WindowActivationError(f"durable context activation failed: {exc}") from exc

    return restore


def _durable_messages(action: OpenWindow) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if action.context:
        for message in action.context.messages:
            thawed = thaw_json(message)
            if not object_fields(thawed):
                raise ValueError("durable context message is not an object")
            messages.append(thawed)
    return messages


__all__ = ["OpenWindow", "ReuseWindow", "WindowAction", "WindowOpenReason", "WindowActivationError", "activate_window"]
