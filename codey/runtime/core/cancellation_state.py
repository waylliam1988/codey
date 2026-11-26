from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol

POLL_INTERVAL = 0.2


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


def _safe_timeout(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        if not isinstance(value, (str, int, float)):
            return 0.0
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    import math as _math
    if not _math.isfinite(number) or number < 0:
        return 0.0
    return number


class _CancellationContext(threading.local):
    def __init__(self) -> None:
        self.event: threading.Event | None = None
        self.deadline: float | None = None


_context = _CancellationContext()


class TaskCancelled(RuntimeError):
    """Raised when the user stops the active task."""


class DeadlineExceeded(TimeoutError):
    """Raised when a bounded provider operation exhausts its total budget."""


def current_event() -> threading.Event | None:
    return _context.event


def current_deadline() -> float | None:
    return _context.deadline


def set_event(event: threading.Event | None) -> threading.Event | None:
    previous = current_event()
    _context.event = event
    return previous


@contextmanager
def scope(event: threading.Event | None) -> Generator[None, None, None]:
    previous = set_event(event)
    try:
        yield
    finally:
        set_event(previous)


@contextmanager
def deadline_scope(deadline: float | None) -> Generator[None, None, None]:
    previous = current_deadline()
    active = deadline
    if previous is not None and (active is None or previous < active):
        active = previous
    _context.deadline = active
    try:
        yield
    finally:
        _context.deadline = previous


@contextmanager
def cleanup_scope() -> Generator[None, None, None]:
    """Allow bounded resource cleanup after its owning operation was cancelled."""
    event, deadline = _context.event, _context.deadline
    _context.event, _context.deadline = None, None
    try:
        yield
    finally:
        _context.event, _context.deadline = event, deadline


class _LinkedEvent(threading.Event):
    """A local stop request also observes its enclosing cancellation sources."""

    def __init__(self, parents: tuple[CancellationSignal, ...]) -> None:
        super().__init__()
        self._parents = parents

    def is_set(self) -> bool:
        return super().is_set() or any(parent.is_set() for parent in self._parents)

    def wait(self, timeout: float | None = None) -> bool:
        end = None if timeout is None else time.monotonic() + _safe_timeout(timeout)
        while not self.is_set():
            remaining = POLL_INTERVAL if end is None else min(POLL_INTERVAL, end - time.monotonic())
            if remaining <= 0:
                return self.is_set()
            super().wait(remaining)
        return True


@contextmanager
def linked_scope(
    event: CancellationSignal | None = None, *, deadline: float | None = None,
) -> Generator[threading.Event, None, None]:
    """Own local cancellation without replacing the enclosing stop or deadline.

    Blocking child threads receive the yielded event and current deadline through
    the ordinary scopes. The owner must join its children before leaving.
    """
    parents = tuple(parent for parent in (current_event(), event) if parent is not None)
    local = _LinkedEvent(parents)
    with scope(local), deadline_scope(deadline):
        try:
            yield local
        finally:
            local.set()


def check() -> None:
    event = current_event()
    if event is not None and event.is_set():
        raise TaskCancelled("task stopped")
    deadline = current_deadline()
    if deadline is not None and time.monotonic() >= deadline:
        raise DeadlineExceeded("provider operation timed out")


def wait(seconds: float) -> None:
    check()
    event = current_event()
    timeout = _safe_timeout(seconds)
    deadline = current_deadline()
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
    if event is None:
        time.sleep(timeout)
    elif event.wait(timeout):
        raise TaskCancelled("task stopped")
    check()
