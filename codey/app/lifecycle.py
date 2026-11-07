"""Own application shutdown order and shell-execution lifetimes."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar, cast

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def release_optional_resource(resource: object) -> None:
    """Some evidence adapters own handles; the file-only store owns none."""
    close = cast(Callable[[], object] | None, getattr(resource, "close", None))
    if callable(close):
        close()


class ResourceSlot(Generic[_T]):
    """Retry a resource release; detach it only after successful cleanup."""

    def __init__(
        self, read: Callable[[], _T | None], release: Callable[[_T], object],
        clear: Callable[[_T], None] | None = None, *, attempts: int = 3,
    ) -> None:
        self._read = read
        self._release = release
        self._clear = clear
        self._attempts = attempts

    def close(self) -> bool:
        resource = self._read()
        if resource is None:
            return True
        for attempt in range(self._attempts):
            try:
                self._release(resource)
            except Exception:
                if attempt == self._attempts - 1:
                    logger.exception("Resource cleanup failed: %s", type(resource).__name__)
                    return False
                time.sleep(0.05 * (attempt + 1))
            else:
                if self._clear is not None:
                    self._clear(resource)
                return True
        return False


class AppLifecycle:
    """Stop admission, drain execution, then release stores in order."""

    def __init__(
        self, *, stop: Callable[[], None], close_connections: Callable[[], None],
        close_conversations: Callable[[], bool], stop_scheduler: Callable[[], bool],
        wait_workers: Callable[[], bool], close_controls: Callable[[], bool],
        cleanup: tuple[Callable[[], bool], ...],
    ) -> None:
        self.close_requested = False
        self.resources_closed = False
        self._condition = threading.Condition()
        self._shell_executions = 0
        self._stop = stop
        self._close_connections = close_connections
        self._close_conversations = close_conversations
        self._stop_scheduler = stop_scheduler
        self._wait_workers = wait_workers
        self._close_controls = close_controls
        self._cleanup = cleanup

    @property
    def shell_executions(self) -> int:
        with self._condition:
            return self._shell_executions

    def begin_shell_execution(self) -> bool:
        with self._condition:
            if self.close_requested:
                return False
            self._shell_executions += 1
            return True

    def end_shell_execution(self) -> None:
        with self._condition:
            self._shell_executions = max(0, self._shell_executions - 1)
            if self._shell_executions == 0:
                self._condition.notify_all()

    def wait_shell_executions(self, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        with self._condition:
            while self._shell_executions:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self) -> bool:
        if self.resources_closed:
            return True
        if not self.close_requested:
            self.close_requested = True
            # Stop uses the same spawn gate as execution's final check/Popen.
            self._stop()
        self._close_connections()
        if not self._close_conversations() or not self._stop_scheduler():
            return False
        if not self._wait_workers() or not self.wait_shell_executions():
            return False
        complete = self._close_controls()
        for cleanup in self._cleanup:
            if not cleanup():
                complete = False
        self.resources_closed = complete
        return complete
