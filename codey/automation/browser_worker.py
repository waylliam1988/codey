"""Run Playwright sync API on one dedicated thread.

Playwright's synchronous Python API is not thread-safe. The UI server runs
HTTP handlers and task dispatch on other threads, so all browser automation
(including provider connect/send and agent runs) must be scheduled here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import queue
import threading
import time
from typing import Any, Callable, TypeVar

from codey.runtime import cancellation

T = TypeVar("T")
_POLL_INTERVAL = 0.05


class _JobState(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CANCELLATION_REQUESTED = "cancellation_requested"


@dataclass
class _Job:
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    slot: list[Any] = field(default_factory=list)
    state: _JobState = _JobState.QUEUED
    cancel_event: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class BrowserWorker:
    def __init__(self, *, name: str = "codey-browser") -> None:
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._thread_id: int | None = None
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._thread_id = threading.get_ident()
        while True:
            job = self._queue.get()
            with job.lock:
                if job.state == _JobState.CANCELLED:
                    job.done.set()
                    continue
                job.state = _JobState.RUNNING

            try:
                with cancellation.scope(job.cancel_event):
                    with cancellation.deadline_scope(job.deadline):
                        job.slot.append(job.fn(*job.args, **job.kwargs))
            except Exception as exc:
                job.slot.append(exc)
            finally:
                with job.lock:
                    if job.state != _JobState.CANCELLED:
                        job.state = _JobState.COMPLETED
                job.done.set()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Schedule work on the browser thread without blocking the caller."""
        job = _Job(fn=fn, args=args, kwargs=kwargs)
        self._queue.put(job)

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

        caller_event = cancellation.current_event()
        caller_deadline = cancellation.current_deadline()
        timeout_deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        if caller_deadline is not None and timeout_deadline is not None:
            active_deadline = min(caller_deadline, timeout_deadline)
        else:
            active_deadline = caller_deadline if caller_deadline is not None else timeout_deadline

        job = _Job(
            fn=fn,
            args=args,
            kwargs=kwargs,
            deadline=active_deadline,
        )

        if caller_event is not None and caller_event.is_set():
            job.cancel_event.set()
            job.state = _JobState.CANCELLED
            raise cancellation.TaskCancelled("task was cancelled before browser job execution")

        self._queue.put(job)

        try:
            while not job.done.wait(_POLL_INTERVAL):
                if caller_event is not None and caller_event.is_set():
                    with job.lock:
                        job.cancel_event.set()
                        if job.state == _JobState.QUEUED:
                            job.state = _JobState.CANCELLED
                        elif job.state == _JobState.RUNNING:
                            job.state = _JobState.CANCELLATION_REQUESTED
                    raise cancellation.TaskCancelled("task was cancelled during browser job execution")
                if active_deadline is not None and time.monotonic() >= active_deadline:
                    with job.lock:
                        job.cancel_event.set()
                        if job.state == _JobState.QUEUED:
                            job.state = _JobState.CANCELLED
                        elif job.state == _JobState.RUNNING:
                            job.state = _JobState.CANCELLATION_REQUESTED
                    raise TimeoutError(
                        "browser worker call timed out: cancellation requested; browser job may finish asynchronously"
                    )
        except Exception:
            with job.lock:
                job.cancel_event.set()
                if job.state == _JobState.QUEUED:
                    job.state = _JobState.CANCELLED
                elif job.state == _JobState.RUNNING:
                    job.state = _JobState.CANCELLATION_REQUESTED
            raise

        result = job.slot[0]
        if isinstance(result, Exception):
            raise result
        return result


WORKER = BrowserWorker()


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    WORKER.submit(fn, *args, **kwargs)


def call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return WORKER.call(fn, *args, **kwargs)