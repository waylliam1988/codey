"""Run Playwright sync API on one dedicated thread.

Playwright's synchronous Python API is not thread-safe. The UI server runs
HTTP handlers and task dispatch on other threads, so all browser automation
(including provider connect/send and agent runs) must be scheduled here.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, TypeVar

from codey.runtime import cancellation

T = TypeVar("T")
_POLL_INTERVAL = 0.1


class BrowserWorker:
    def __init__(self, *, name: str = "codey-browser") -> None:
        self._queue: queue.Queue[
            tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], threading.Event, list[Any]]
        ] = queue.Queue()
        self._thread_id: int | None = None
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._thread_id = threading.get_ident()
        while True:
            fn, args, kwargs, done, slot = self._queue.get()
            try:
                slot.append(fn(*args, **kwargs))
            except Exception as exc:
                slot.append(exc)
            finally:
                done.set()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Schedule work on the browser thread without blocking the caller."""
        self._queue.put((fn, args, kwargs, threading.Event(), []))

    def call(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> T:
        """Run work on the browser thread and wait for the result."""
        if threading.get_ident() == self._thread_id:
            return fn(*args, **kwargs)
        done = threading.Event()
        slot: list[Any] = []
        self._queue.put((fn, args, kwargs, done, slot))
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not done.wait(_POLL_INTERVAL):
            cancellation.check()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("browser worker call timed out")
        result = slot[0]
        if isinstance(result, Exception):
            raise result
        return result


WORKER = BrowserWorker()


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    WORKER.submit(fn, *args, **kwargs)


def call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return WORKER.call(fn, *args, **kwargs)