"""Run Playwright sync API on one dedicated thread.

Playwright's synchronous Python API is not thread-safe. The UI server runs
HTTP handlers and task dispatch on other threads, so all browser automation
(including provider connect/send and agent runs) must be scheduled here.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class BrowserWorker:
    def __init__(self) -> None:
        self._queue: queue.Queue[
            tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], threading.Event, list[Any]]
        ] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="codey-browser", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
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

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run work on the browser thread and wait for the result."""
        done = threading.Event()
        slot: list[Any] = []
        self._queue.put((fn, args, kwargs, done, slot))
        done.wait()
        result = slot[0]
        if isinstance(result, Exception):
            raise result
        return result


WORKER = BrowserWorker()


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    WORKER.submit(fn, *args, **kwargs)
