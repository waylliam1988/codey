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
from codey.durable.errors import SchemaVersionError
from codey.durable.model import (
    TASK_TERMINAL_STATUSES,
    CommitResult,
    DurableMutation,
    TaskCheckpoint,
)


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
        database_path = Path(database_path)
        try:
            session = DurableSession(SQLiteDurableStore(database_path))
        except SchemaVersionError:
            # The application is intentionally cold-start only.  There is no
            # durable migration or legacy-history compatibility layer: an old
            # database is discarded and replaced with the current schema.
            # SQLite may leave WAL/SHM/journal sidecars behind, so remove all
            # of them before opening the fresh store.
            for suffix in ("", "-wal", "-shm", "-journal"):
                database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)
            session = DurableSession(SQLiteDurableStore(database_path))
        scheduler = DurableTaskScheduler(session, dispatch)
        def observe(result: CommitResult, mutation: DurableMutation) -> None:
            del result
            if mutation.task is None:
                if mutation.submission is not None and mutation.submission.mode == "write":
                    scheduler.notify()
                return
            if mutation.task.status in TASK_TERMINAL_STATUSES:
                # A terminal boundary may free a conversation for a queued
                # submission whose task is not the one being committed.
                scheduler.notify()
                return
            if any(key in mutation.task.state for key in ("input_identity", "resume_input", "workflow_origin", "input_state")):
                scheduler.notify(mutation.task.task_id)

        session.observe_commits(observe)
        if publish is not None:
            session.subscribe_commits(publish)
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
