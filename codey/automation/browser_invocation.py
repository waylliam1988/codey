"""Carry the admitted kernel effect through synchronous browser subcommands."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from codey.types.json_value import JsonObject


@dataclass(frozen=True)
class BrowserInvocation:
    effect_ref: str
    progress: Callable[[JsonObject], None] | None = None


_current: ContextVar[BrowserInvocation | None] = ContextVar("browser_invocation", default=None)


def current_invocation() -> BrowserInvocation | None:
    return _current.get()


@contextmanager
def invocation_scope(invocation: BrowserInvocation) -> Generator[None, None, None]:
    token = _current.set(invocation)
    try:
        yield
    finally:
        _current.reset(token)
