"""Lifecycle ownership for the durable session and its dispatcher.

This is deliberately a small lifecycle object, not a service container.  The
session remains the owner of durable facts and the scheduler remains the owner
of volatile wakeups; this object only makes their startup and shutdown order
explicit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codey.durable import DurableSession, DurableTaskScheduler, SQLiteDurableStore
from codey.durable.model import CommitResult, DurableMutation, TaskCheckpoint


@dataclass
class DurableAppServices:
    """Own one durable session and its restartable task scheduler."""

    session: DurableSession
    scheduler: DurableTaskScheduler
    _scheduler_closed: bool = False
    _session_closed: bool = False

    @classmethod
    def open(
        cls,
        database_path: str | Path,
        *,
        publish: Callable[[CommitResult, DurableMutation], None] | None,
        dispatch: Callable[[TaskCheckpoint], bool],
        start: bool = True,
    ) -> DurableAppServices:
        session = DurableSession(
            SQLiteDurableStore(database_path),
            on_commit=publish,
        )
        scheduler = DurableTaskScheduler(session, dispatch)
        services = cls(session, scheduler)
        if start:
            scheduler.start()
        return services

    def stop_scheduler(self, timeout: float = 5.0) -> bool:
        """Stop volatile dispatch before any durable resource is released."""
        if self._scheduler_closed:
            return True
        if not self.scheduler.close(timeout):
            return False
        self._scheduler_closed = True
        return True

    def close(self, timeout: float = 5.0) -> bool:
        """Close scheduler first, then the session, preserving retryability."""
        if not self.stop_scheduler(timeout):
            return False
        if not self._session_closed:
            self.session.close()
            self._session_closed = True
        return True


__all__ = ["DurableAppServices"]
