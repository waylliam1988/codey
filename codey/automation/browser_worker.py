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
DEFAULT_STUCK_AFTER_SECONDS = 30.0


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


@dataclass(frozen=True)
class BrowserWorkerHealth:
    state: str
    queue_size: int
    current_job_state: str
    running_for_seconds: float
    stuck_after_seconds: float
    stuck_detected: bool
    completed_jobs: int
    failed_jobs: int
    cancelled_jobs: int
    thread_alive: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "queue_size": self.queue_size,
            "current_job_state": self.current_job_state,
            "running_for_seconds": round(self.running_for_seconds, 3),
            "stuck_after_seconds": round(self.stuck_after_seconds, 3),
            "stuck_detected": self.stuck_detected,
            "completed_jobs": self.completed_jobs,
            "failed_jobs": self.failed_jobs,
            "cancelled_jobs": self.cancelled_jobs,
            "thread_alive": self.thread_alive,
        }


class BrowserWorker:
    def __init__(
        self,
        *,
        name: str = "codey-browser",
        stuck_after_seconds: float = DEFAULT_STUCK_AFTER_SECONDS,
    ) -> None:
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._thread_id: int | None = None
        self._stuck_after_seconds = max(_POLL_INTERVAL, float(stuck_after_seconds))
        self._state_lock = threading.Lock()
        self._current_job: _Job | None = None
        self._current_started_at = 0.0
        self._completed_jobs = 0
        self._failed_jobs = 0
        self._cancelled_jobs = 0
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._thread_id = threading.get_ident()
        while True:
            job = self._queue.get()
            with job.lock:
                if job.state == _JobState.CANCELLED:
                    with self._state_lock:
                        self._cancelled_jobs += 1
                    job.done.set()
                    continue
                job.state = _JobState.RUNNING
            with self._state_lock:
                self._current_job = job
                self._current_started_at = time.monotonic()

            failed = False
            try:
                with cancellation.scope(job.cancel_event):
                    with cancellation.deadline_scope(job.deadline):
                        job.slot.append(job.fn(*job.args, **job.kwargs))
            except Exception as exc:
                failed = True
                job.slot.append(exc)
            finally:
                with job.lock:
                    if job.state != _JobState.CANCELLED:
                        job.state = _JobState.COMPLETED
                    final_state = job.state
                with self._state_lock:
                    if self._current_job is job:
                        self._current_job = None
                        self._current_started_at = 0.0
                    if final_state == _JobState.CANCELLED:
                        self._cancelled_jobs += 1
                    elif failed:
                        self._failed_jobs += 1
                    else:
                        self._completed_jobs += 1
                job.done.set()

    def health_snapshot(self, *, now: float | None = None) -> BrowserWorkerHealth:
        """Return passive worker health without restarting or mutating jobs."""

        observed_at = time.monotonic() if now is None else float(now)
        with self._state_lock:
            current = self._current_job
            started_at = self._current_started_at
            completed_jobs = self._completed_jobs
            failed_jobs = self._failed_jobs
            cancelled_jobs = self._cancelled_jobs
        current_state = ""
        running_for = 0.0
        stuck = False
        state = "idle"
        if current is not None:
            with current.lock:
                job_state = current.state
            current_state = job_state.value
            if started_at:
                running_for = max(0.0, observed_at - started_at)
            stuck = job_state == _JobState.CANCELLATION_REQUESTED and running_for >= self._stuck_after_seconds
            state = "stuck" if stuck else job_state.value
        return BrowserWorkerHealth(
            state=state,
            queue_size=max(0, int(self._queue.qsize())),
            current_job_state=current_state,
            running_for_seconds=running_for,
            stuck_after_seconds=self._stuck_after_seconds,
            stuck_detected=stuck,
            completed_jobs=completed_jobs,
            failed_jobs=failed_jobs,
            cancelled_jobs=cancelled_jobs,
            thread_alive=self._thread.is_alive(),
        )

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
        if threading.get_ident() == self._thread_id:
            caller_event = cancellation.current_event()
            caller_deadline = cancellation.current_deadline()
            timeout_deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            if caller_deadline is not None and timeout_deadline is not None:
                active_deadline = min(caller_deadline, timeout_deadline)
            else:
                active_deadline = caller_deadline if caller_deadline is not None else timeout_deadline

            with cancellation.scope(caller_event):
                with cancellation.deadline_scope(active_deadline):
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


def health_snapshot() -> BrowserWorkerHealth:
    return WORKER.health_snapshot()
