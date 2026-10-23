"""Disposable startup observations, independent of service/task facts."""
from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from codey.storage.file_lock import LockTimeout, acquire_lease

_trace: ContextVar[StartupTrace | None] = ContextVar("startup_trace", default=None)


class StartupTrace:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.identity = uuid.uuid4().hex
        self.started = time.perf_counter()

    def record(self, stage: str, *, elapsed: float = 0, outcome: str = "complete") -> None:
        row = {"startup_id": self.identity, "process_id": os.getpid(), "stage": stage, "outcome": outcome,
               "elapsed_seconds": round(elapsed, 6), "since_start_seconds": round(time.perf_counter() - self.started, 6),
               "time": time.time()}
        # Logs are observations. An unwritable diagnostic path cannot stop a launch.
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            with acquire_lease(self.home / "startup-timings", timeout_seconds=1):
                path = self.home / "startup-timings.jsonl"
                if path.exists() and path.stat().st_size > 1024 * 1024:
                    path.replace(self.home / "startup-timings.previous.jsonl")
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")
        except (OSError, LockTimeout):
            pass

    @contextmanager
    def activate(self) -> Generator[None, None, None]:
        token = _trace.set(self)
        try:
            yield
        finally:
            _trace.reset(token)


@contextmanager
def startup_stage(name: str) -> Generator[None, None, None]:
    trace = _trace.get()
    started = time.perf_counter()
    try:
        yield
    except BaseException:
        if trace:
            trace.record(name, elapsed=time.perf_counter() - started, outcome="failed")
        raise
    else:
        if trace:
            trace.record(name, elapsed=time.perf_counter() - started)
