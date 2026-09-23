"""Fail-open lifecycle hooks for the agent runtime.

Hooks are observability/extension points only. A hook must never change task
semantics: any exception is swallowed and only recorded via the optional
``on_error`` callback so a broken hook cannot fail a run.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeHooks:
    before_provider_send: tuple[Callable[..., None], ...] = ()
    after_provider_send: tuple[Callable[..., None], ...] = ()
    before_tool_call: tuple[Callable[..., None], ...] = ()
    after_tool_call: tuple[Callable[..., None], ...] = ()
    on_turn_end: tuple[Callable[..., None], ...] = ()


def call_hooks(
    hooks: RuntimeHooks | None,
    name: str,
    *,
    on_error: Callable[[str, Exception], None] | None = None,
    **payload: Any,
) -> None:
    if hooks is None:
        return
    handlers = getattr(hooks, name, ())
    if not handlers:
        return
    for handler in handlers:
        try:
            handler(**payload)
        except Exception as exc:  # noqa: BLE001 - hooks are fail-open by design
            if on_error is not None:
                with contextlib.suppress(Exception):
                    on_error(name, exc)


__all__ = ["RuntimeHooks", "call_hooks"]
