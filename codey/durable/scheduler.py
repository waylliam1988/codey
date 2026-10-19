"""Small restartable dispatcher for durable task admission.

The scheduler owns no task state. SQLite owns the checkpoint; this object only
keeps a bounded in-memory wakeup set and asks the host to dispatch every
non-terminal checkpoint. A false dispatch result leaves the task durable and
eligible for retry; the logical phase is never replaced by a synthetic task.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextlib import suppress

from codey.durable.errors import CommitConflict
from codey.durable.model import TaskCheckpoint, TaskStatus
from codey.durable.session import DurableSession
from codey.durable.workflow import WorkflowDefinition


class DurableTaskScheduler:
    def __init__(
        self,
        session: DurableSession,
        dispatch: Callable[[TaskCheckpoint], bool],
        *,
        retry_interval: float = 0.25,
        workflows: Mapping[str, WorkflowDefinition] | None = None,
    ) -> None:
        self._session = session
        self._dispatch = dispatch
        self._retry_interval = max(0.01, float(retry_interval))
        self._workflows = dict(workflows or {})
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._pending: set[str] = set()
        self._inflight: set[str] = set()
        self._scheduled: set[str] = set()
        self._blocked: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._recovered = False
        self._enabled = False

    @property
    def in_flight(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._inflight))

    @property
    def blocked_tasks(self) -> dict[str, str]:
        """Return derived reasons for tasks the current registry cannot run."""
        with self._lock:
            return dict(self._blocked)

    def set_workflows(self, workflows: Mapping[str, WorkflowDefinition]) -> None:
        """Replace the registry snapshot and reconsider previously blocked work."""
        with self._lock:
            self._workflows = dict(workflows)
            self._blocked.clear()
        self.notify()

    def start(self) -> None:
        self._enabled = True
        if not self._recovered:
            self._session.recover_scheduler_state()
            self._recovered = True
        self._session.reconcile_finalization()
        self.notify()
        self._ensure_thread()

    def notify(self, task_id: str | None = None, *, start: bool = True) -> None:
        pending: set[str] | None = None
        requeued = False
        if task_id is None:
            tasks = self._session.pending_tasks()
            pending = {task.task_id for task in tasks if task.status == TaskStatus.PENDING}
        else:
            try:
                task = self._session.task(task_id)
                requeued = task.status == TaskStatus.PENDING and not task.attempt_token
            except KeyError:
                return
        with self._lock:
            if task_id is None:
                assert pending is not None
                self._scheduled.intersection_update(pending)
                self._pending.update(pending - self._scheduled)
            else:
                if requeued:
                    self._scheduled.discard(task_id)
                if task_id not in self._scheduled:
                    self._pending.add(task_id)
        self._wake.set()
        if start:
            self._ensure_thread()

    def retry(self, task_id: str) -> None:
        with self._lock:
            self._scheduled.discard(task_id)
            self._pending.add(task_id)
        self._wake.set()
        self._ensure_thread()

    def claim(self, task_id: str) -> None:
        """Mark an already queued task as owned by the local worker.

        The host must already have claimed a durable attempt. This merely
        suppresses duplicate local wakeups while that attempt is queued.
        """
        with self._lock:
            self._pending.discard(task_id)
            self._scheduled.add(task_id)

    def close(self, timeout: float = 5.0) -> bool:
        self._enabled = False
        self._closed.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        try:
            while not self._closed.is_set():
                self._wake.wait(self._retry_interval)
                self._wake.clear()
                if self._closed.is_set():
                    return
                self._reconcile()
                with self._lock:
                    if not self._pending:
                        return
        finally:
            with self._lock:
                self._thread = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._enabled or not self._pending or self._closed.is_set():
                return
            self._closed.clear()
            self._thread = threading.Thread(target=self._run, name="codey-durable-scheduler", daemon=True)
            self._thread.start()

    def _reconcile(self) -> None:
        if self._workflows:
            self._session.reconcile_workflows(self._workflows)
        self._session.reconcile_finalization()
        self.notify(start=False)
        with self._lock:
            candidates = tuple(sorted(self._pending - self._inflight - self._scheduled))
        for task_id in candidates:
            if self._closed.is_set():
                return
            with self._lock:
                self._inflight.add(task_id)
            try:
                task = self._session.task(task_id)
                block_reason = self._workflow_block_reason(task)
                if block_reason is not None:
                    # A missing, newer, or migration-incompatible workflow is
                    # durably inspectable but must never be handed to a
                    # worker that cannot interpret its checkpoint.
                    with self._lock:
                        self._blocked[task_id] = block_reason
                    self._remove(task_id)
                    continue
                with self._lock:
                    self._blocked.pop(task_id, None)
                if task.state.get("cancel_requested"):
                    self._session.cancel_task(
                        task_id,
                        reason=str(task.state.get("cancel_reason") or "cancelled"),
                    )
                    self._remove(task_id)
                    continue
                if task.status != TaskStatus.PENDING:
                    self._remove(task_id)
                    continue
                try:
                    self._session.claim_task(task_id)
                except (CommitConflict, KeyError, ValueError):
                    self._remove(task_id)
                    continue
                claimed = self._session.task(task_id)
                with self._session.execution(task_id, claimed.attempt_token):
                    accepted = bool(self._dispatch(claimed))
                if accepted:
                    current = self._session.task(task_id)
                    with self._lock:
                        if current.status == TaskStatus.PENDING:
                            self._scheduled.discard(task_id)
                            self._pending.add(task_id)
                        else:
                            self._pending.discard(task_id)
                            self._scheduled.add(task_id)
                else:
                    self._session.unclaim_task(task_id)
                    self._defer_retry(task_id)
            except (KeyError, ValueError):
                # A terminal or deleted task is no longer schedulable. The
                # durable record remains available for audit and inspection.
                self._remove(task_id)
            except Exception:
                # Dispatch errors are retried; the durable checkpoint remains
                # the only authority and no speculative in-memory state is kept.
                with suppress(KeyError, ValueError, CommitConflict):
                    self._session.unclaim_task(task_id)
                self._defer_retry(task_id)
            finally:
                with self._lock:
                    self._inflight.discard(task_id)

    def _defer_retry(self, task_id: str) -> None:
        with self._lock:
            self._scheduled.discard(task_id)
            self._pending.add(task_id)

    def _remove(self, task_id: str) -> None:
        with self._lock:
            self._pending.discard(task_id)

    def _workflow_block_reason(self, task: TaskCheckpoint) -> str | None:
        name = task.workflow_name
        if name is None:
            return None
        definition = self._workflows.get(name)
        if definition is None:
            return "missing_task"
        if task.workflow_version > definition.version:
            return "task_too_old"
        if task.workflow_version < definition.version:
            return "migration_failed"
        contract = task.state.get("workflow_replay_contract", "deterministic-v1")
        if contract != definition.replay_contract:
            return "migration_failed"
        from codey.durable.workflow import workflow_digest

        if task.replay_digest != workflow_digest(
            definition.name,
            definition.version,
            task.workflow_input,
            task.workflow_checkpoint,
        ):
            return "migration_failed"
        return None


__all__ = ["DurableTaskScheduler"]
