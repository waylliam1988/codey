"""Small restartable dispatcher for durable task admission.

The scheduler owns no task state. SQLite owns the checkpoint; this object only
keeps a bounded in-memory wakeup set and asks the host to dispatch every
non-terminal checkpoint. A false dispatch result leaves the task durable and
eligible for retry; the logical phase is never replaced by a synthetic task.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from codey.durable.errors import CommitConflict
from codey.durable.model import CommitResult, TaskCheckpoint, TaskStatus, kernel_cleanup_required
from codey.durable.session import DurableSession
from codey.durable.workflow import WorkflowDefinition

_LOGGER = logging.getLogger(__name__)


@dataclass
class TaskInvocation:
    """Volatile dispatch invocation bound to one durable attempt."""

    task_id: str
    attempt_token: str
    mode: str
    signal: threading.Event
    done: threading.Event


@dataclass
class AbortInvocation:
    """Volatile cleanup invocation started after a durable abort barrier."""

    task_id: str
    attempt_token: str
    signal: threading.Event
    done: threading.Event
    error: BaseException | None = None


@dataclass(frozen=True)
class SchedulerFailure:
    """Structured scheduler failure retained for operators and tests."""

    phase: str
    error_type: str
    message: str
    retry_count: int
    task_id: str = ""


class DurableTaskScheduler:
    def __init__(
        self,
        session: DurableSession,
        dispatch: Callable[[TaskCheckpoint], bool],
        *,
        abort_handler: Callable[[TaskCheckpoint, AbortInvocation], None] | None = None,
        retry_interval: float = 0.25,
        workflows: Mapping[str, WorkflowDefinition] | None = None,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> None:
        self._session = session
        self._dispatch = dispatch
        self._abort_handler = abort_handler
        self._retry_interval = max(0.01, float(retry_interval))
        self._workflows = dict(workflows or {})
        if steering_mode not in {"all", "one-at-a-time"}:
            raise ValueError("invalid steering queue mode")
        if follow_up_mode not in {"all", "one-at-a-time"}:
            raise ValueError("invalid follow-up queue mode")
        self._steering_mode = steering_mode
        self._follow_up_mode = follow_up_mode
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._pending: set[str] = set()
        self._inflight: set[str] = set()
        self._scheduled: set[str] = set()
        self._blocked: dict[str, str] = {}
        self._task_failures: dict[str, SchedulerFailure] = {}
        self._loop_failure: SchedulerFailure | None = None
        self._loop_failure_count = 0
        self._loop_failure_limit = 3
        self._invocations: dict[str, TaskInvocation] = {}
        self._abort_invocations: dict[str, AbortInvocation] = {}
        self._reconcile_requested = False
        self._thread: threading.Thread | None = None
        self._recovered = False
        self._enabled = False
        self._owner_id = f"scheduler-{uuid.uuid4().hex}"
        self._owns_scheduler = False

    @property
    def in_flight(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._inflight))

    @property
    def blocked_tasks(self) -> dict[str, str]:
        """Return derived reasons for tasks the current registry cannot run."""
        with self._lock:
            return dict(self._blocked)

    @property
    def task_failures(self) -> dict[str, SchedulerFailure]:
        with self._lock:
            return dict(self._task_failures)

    @property
    def loop_failure(self) -> SchedulerFailure | None:
        with self._lock:
            return self._loop_failure

    @property
    def enabled(self) -> bool:
        """Whether the scheduler is currently allowed to start a worker."""
        with self._lock:
            return self._enabled

    def health(self) -> dict[str, object]:
        """Return bounded scheduler state for service health and operators."""
        with self._lock:
            loop_failure = self._loop_failure
            return {
                "enabled": self._enabled,
                "loop_failure": (
                    {
                        "phase": loop_failure.phase,
                        "error_type": loop_failure.error_type,
                        "message": loop_failure.message,
                        "retry_count": loop_failure.retry_count,
                    }
                    if loop_failure is not None
                    else None
                ),
                "task_failures": {
                    task_id: {
                        "phase": failure.phase,
                        "error_type": failure.error_type,
                        "message": failure.message,
                        "retry_count": failure.retry_count,
                    }
                    for task_id, failure in self._task_failures.items()
                },
                "blocked_tasks": dict(self._blocked),
            }

    def set_workflows(self, workflows: Mapping[str, WorkflowDefinition]) -> None:
        """Replace the registry snapshot and reconsider previously blocked work."""
        with self._lock:
            self._workflows = dict(workflows)
            self._blocked.clear()
        self.notify()

    def invocation(self, task_id: str) -> TaskInvocation | None:
        with self._lock:
            return self._invocations.get(task_id)

    def abort_invocation(self, task_id: str) -> AbortInvocation | None:
        with self._lock:
            return self._abort_invocations.get(task_id)

    def abort_task(self, task_id: str, *, reason: str = "cancelled") -> CommitResult:
        """Durably mark cancellation, then run the host abort barrier.

        A task can be executing in a host-owned worker after the scheduler's
        short dispatch invocation has returned.  The durable cancellation
        still needs to reach that worker's physical resources, so the abort
        handler also runs when no scheduler invocation is present.
        """
        result = self._session.request_cancel(task_id, reason=reason)
        with self._lock:
            invocation = self._invocations.get(task_id)
        handler = self._abort_handler
        if invocation is not None:
            invocation.signal.set()
            if handler is not None:
                with self._lock:
                    existing = self._abort_invocations.get(task_id)
                    if existing is None:
                        abort_invocation = AbortInvocation(
                            task_id=task_id,
                            attempt_token=invocation.attempt_token,
                            signal=threading.Event(),
                            done=threading.Event(),
                        )
                        self._abort_invocations[task_id] = abort_invocation
                    else:
                        abort_invocation = existing
                if existing is None:
                    thread = threading.Thread(
                        target=self._run_abort_handler,
                        args=(abort_invocation, handler),
                        name=f"codey-abort-{task_id}",
                        daemon=True,
                    )
                    thread.start()
                abort_invocation.done.wait()
                if abort_invocation.error is not None:
                    raise abort_invocation.error
        elif handler is not None:
            try:
                checkpoint = self._session.task(task_id)
            except KeyError:
                checkpoint = None
            if checkpoint is not None:
                # Host workers may outlive the scheduler dispatch call.  Use
                # the durable attempt token to keep the abort callback's
                # identity explicit even without a volatile invocation.
                abort_invocation = AbortInvocation(
                    task_id=task_id,
                    attempt_token=checkpoint.attempt_token,
                    signal=threading.Event(),
                    done=threading.Event(),
                )
                handler(checkpoint, abort_invocation)
        return result

    def _run_abort_handler(
        self,
        invocation: AbortInvocation,
        handler: Callable[[TaskCheckpoint, AbortInvocation], None],
    ) -> None:
        try:
            handler(self._session.task(invocation.task_id), invocation)
        except BaseException as exc:
            invocation.error = exc
        finally:
            invocation.done.set()
            with self._lock:
                if self._abort_invocations.get(invocation.task_id) is invocation:
                    self._abort_invocations.pop(invocation.task_id, None)

    def start(self) -> None:
        resumed = False
        with self._lock:
            if self._owns_scheduler:
                if self._enabled:
                    return
                # A bounded failure stops the worker but deliberately keeps
                # durable ownership.  Starting again is the explicit operator
                # recovery operation for that state.
                self._enabled = True
                self._loop_failure_count = 0
                self._reconcile_requested = True
                resumed = True
        if not resumed:
            self._session.store.acquire_scheduler_owner(self._owner_id, os.getpid())
            with self._lock:
                self._owns_scheduler = True
                self._enabled = True
        try:
            if not resumed and not self._recovered:
                self._session.recover_scheduler_state()
                self._recovered = True
            self._session.reconcile_finalization()
            self.notify()
            self._ensure_thread()
        except BaseException:
            with self._lock:
                if not resumed:
                    self._owns_scheduler = False
                self._enabled = False
            if not resumed:
                self._session.store.release_scheduler_owner(self._owner_id)
            raise

    def resume(self) -> None:
        """Resume a scheduler stopped by its bounded failure circuit breaker."""
        self.start()

    def notify(
        self,
        task_id: str | None = None,
        *,
        start: bool = True,
        include_queued_submissions: bool = True,
    ) -> None:
        pending: set[str] | None = None
        requeued = False
        dependents: set[str] = set()
        queued_submissions = False
        if task_id is None:
            tasks = self._session.pending_tasks()
            pending = {task.task_id for task in tasks if task.status == TaskStatus.PENDING}
            if include_queued_submissions:
                queued_submissions = bool(self._session.queued_submissions())
        else:
            try:
                task = self._session.task(task_id)
                requeued = task.status == TaskStatus.PENDING and not task.attempt_token
                dependents = {
                    candidate.task_id
                    for candidate in self._session.store.waiting_dependents(task_id)
                    if candidate.status == TaskStatus.WAITING
                }
            except KeyError:
                return
        with self._lock:
            if task_id is None:
                assert pending is not None
                self._scheduled.intersection_update(pending)
                self._pending.update(pending - self._scheduled)
                self._reconcile_requested = self._reconcile_requested or queued_submissions
            else:
                if requeued:
                    self._scheduled.discard(task_id)
                if requeued and task_id not in self._scheduled:
                    self._pending.add(task_id)
                for dependent in dependents:
                    self._scheduled.discard(dependent)
                    self._pending.add(dependent)
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
        with self._lock:
            self._enabled = False
            self._closed.set()
            invocations = tuple(self._invocations.values())
            abort_invocations = tuple(self._abort_invocations.values())
            thread = self._thread
        self._wake.set()
        for invocation in invocations:
            # Closing the scheduler is a lifecycle boundary, not a durable
            # cancellation request. Signal workers so cooperative dispatches
            # can unwind, while the durable attempt remains recoverable.
            invocation.signal.set()
        for abort_invocation in abort_invocations:
            abort_invocation.signal.set()
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        closed = thread is None or not thread.is_alive()
        if closed:
            deadline = time.monotonic() + max(0.0, float(timeout))
            for abort_invocation in abort_invocations:
                abort_invocation.done.wait(max(0.0, deadline - time.monotonic()))
            closed = all(abort_invocation.done.is_set() for abort_invocation in abort_invocations)
        if closed:
            with self._lock:
                owns_scheduler = self._owns_scheduler
                self._owns_scheduler = False
            if owns_scheduler:
                self._session.store.release_scheduler_owner(self._owner_id)
        return closed

    def _run(self) -> None:
        try:
            while not self._closed.is_set():
                self._wake.wait(self._retry_interval)
                self._wake.clear()
                if self._closed.is_set():
                    return
                try:
                    self._reconcile()
                except Exception as exc:
                    with self._lock:
                        self._loop_failure_count += 1
                        failure = SchedulerFailure(
                            "reconcile", type(exc).__name__, str(exc), self._loop_failure_count
                        )
                        self._loop_failure = failure
                        if failure.retry_count >= self._loop_failure_limit:
                            self._enabled = False
                        self._reconcile_requested = True
                    _LOGGER.error(
                        "durable scheduler reconcile failed",
                        extra={
                            "phase": failure.phase,
                            "error_type": failure.error_type,
                            "retry_count": failure.retry_count,
                        },
                        exc_info=exc,
                    )
                    if failure.retry_count >= self._loop_failure_limit:
                        return
                    continue
                with self._lock:
                    # The retry budget is consecutive.  A successful pass
                    # gives a later transient failure a fresh budget while the
                    # last failure remains available for operator inspection.
                    self._loop_failure_count = 0
                    if not self._pending and not self._reconcile_requested:
                        # Retire under the same lock used to start a worker.
                        # A later notification must see the vacant slot even
                        # while this thread is still unwinding its finally.
                        self._thread = None
                        return
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._enabled or (not self._pending and not self._reconcile_requested) or self._closed.is_set():
                return
            thread = threading.Thread(target=self._run, name="codey-durable-scheduler", daemon=True)
            self._thread = thread
            # Publish and start atomically: another notifier or close() must
            # never observe an unstarted thread as a free slot or join it.
            try:
                thread.start()
            except BaseException:
                self._thread = None
                raise

    def _reconcile(self) -> None:
        with self._lock:
            self._reconcile_requested = False
        if self._workflows:
            self._session.reconcile_workflows(self._workflows)
        self._place_ready_inputs()
        self.notify(start=False, include_queued_submissions=False)
        finalization = self._session.store.finalization_queue()
        self._session.reconcile_finalization(finalization)
        with self._lock:
            candidates = tuple(sorted(self._pending - self._inflight - self._scheduled))
        for task_id in candidates:
            if self._closed.is_set():
                return
            with self._lock:
                self._inflight.add(task_id)
                # Consume this wakeup before dispatch. A later pending entry
                # then represents a new durable requeue, not this invocation.
                # Skip/error paths must preserve it too: their checkpoint or
                # claim failure may predate an already committed continuation.
                self._pending.discard(task_id)
            try:
                self._reconcile_task(task_id)
            except Exception as exc:
                # Task-local failures must not disappear with this wakeup. The
                # task was already removed from _pending above, so retain the
                # durable task and make the next pass retry it.
                self._finish_task_error(task_id, exc, phase="read")
            finally:
                with self._lock:
                    self._inflight.discard(task_id)

    def _reconcile_task(self, task_id: str) -> None:
        try:
            task = self._session.task(task_id)
        except KeyError:
            return
        except Exception as exc:
            self._finish_task_error(task_id, exc, phase="read")
            return
        if task.state.get("input_state") == "queued":
            # The inbox boundary owns placement. A queued input is a durable
            # submission, not a runnable task.
            return
        block_reason = self._workflow_block_reason(task)
        if block_reason is not None:
            # A missing, newer, or migration-incompatible workflow is durably
            # inspectable but must never be handed to an incompatible worker.
            with self._lock:
                self._blocked[task_id] = block_reason
            return
        with self._lock:
            self._blocked.pop(task_id, None)
        if task.state.get("cancel_requested") and not kernel_cleanup_required(task):
            self._session.cancel_task(
                task_id,
                reason=str(task.state.get("cancel_reason") or "cancelled"),
            )
            return
        if task.status != TaskStatus.PENDING:
            return
        try:
            self._session.claim_task(task_id)
        except (CommitConflict, KeyError, ValueError):
            return
        except Exception as exc:
            self._finish_task_error(task_id, exc, phase="claim")
            return
        try:
            claimed = self._session.task(task_id)
        except KeyError:
            return
        except Exception as exc:
            # claim_task has committed the attempt, so the next durable read
            # identifies and releases only this invocation's running attempt.
            self._finish_task_error(task_id, exc, phase="claim", claimed=True)
            return
        invocation = TaskInvocation(
            task_id=task_id,
            attempt_token=claimed.attempt_token,
            mode="run",
            signal=threading.Event(),
            done=threading.Event(),
        )
        with self._lock:
            self._invocations[task_id] = invocation
        accepted = False
        dispatch_error: Exception | None = None
        try:
            try:
                with self._session.execution(task_id, claimed.attempt_token):
                    accepted = bool(self._dispatch(claimed))
            except Exception as exc:
                # Dispatch exceptions, including malformed-input ValueError,
                # are task failures rather than deleted-task races.
                dispatch_error = exc
        finally:
            invocation.done.set()
            with self._lock:
                if self._invocations.get(task_id) is invocation:
                    self._invocations.pop(task_id, None)
        if dispatch_error is not None:
            self._finish_dispatch_error(task_id, claimed.attempt_token, dispatch_error)
            return
        if accepted:
            try:
                self._finish_accepted_dispatch(task_id)
            except Exception as exc:
                self._finish_dispatch_error(task_id, claimed.attempt_token, exc)
            return
        self._unclaim_if_owned(task_id, claimed.attempt_token)
        self._defer_retry(task_id)

    def _finish_task_error(
        self,
        task_id: str,
        error: Exception,
        *,
        phase: str,
        claimed: bool = False,
    ) -> None:
        if claimed:
            self._unclaim_current_attempt(task_id)
        if not self._record_task_failure(task_id, error, phase=phase):
            self._defer_retry(task_id)

    def _finish_dispatch_error(self, task_id: str, attempt_token: str, error: Exception) -> None:
        if not self._record_task_failure(task_id, error, phase="dispatch", attempt_token=attempt_token):
            self._unclaim_if_owned(task_id, attempt_token)
            self._defer_retry(task_id)

    def _finish_accepted_dispatch(self, task_id: str) -> None:
        with self._lock:
            self._task_failures.pop(task_id, None)
        try:
            current = self._session.task(task_id)
        except KeyError:
            return
        if current.state.get("cancel_requested") and not kernel_cleanup_required(current):
            self._session.cancel_task(
                task_id,
                reason=str(current.state.get("cancel_reason") or "cancelled"),
            )
            return
        with self._lock:
            # A committed resume may already have notified us; preserve that
            # newer pending observation over the old dispatch snapshot.
            if current.status == TaskStatus.PENDING or task_id in self._pending:
                self._scheduled.discard(task_id)
                self._pending.add(task_id)
            else:
                self._pending.discard(task_id)
                self._scheduled.add(task_id)

    def _record_task_failure(
        self,
        task_id: str,
        error: Exception,
        *,
        phase: str,
        attempt_token: str | None = None,
    ) -> bool:
        """Record a task cause and fail the task after bounded retries."""
        with self._lock:
            previous = self._task_failures.get(task_id)
            retry_count = previous.retry_count + 1 if previous is not None else 1
            failure = SchedulerFailure(phase, type(error).__name__, str(error), retry_count, task_id)
            self._task_failures[task_id] = failure
        _LOGGER.error(
            "durable scheduler task handling failed",
            extra={
                "phase": failure.phase,
                "task_id": task_id,
                "error_type": failure.error_type,
                "retry_count": failure.retry_count,
            },
            exc_info=error,
        )
        if retry_count < self._loop_failure_limit:
            return False
        reason = (
            f"scheduler {phase} failed after {retry_count} attempts: "
            f"{type(error).__name__}: {error}"
        )
        try:
            current = self._session.task(task_id)
            if attempt_token is not None and current.attempt_token != attempt_token:
                return True
            if attempt_token is None and current.status is TaskStatus.RUNNING:
                return True
            self._session.fail_task(
                task_id,
                reason,
                state={**dict(current.state), "scheduler_failure": reason},
                expected_attempt_token=attempt_token if attempt_token is not None else current.attempt_token,
            )
        except (CommitConflict, KeyError, ValueError) as exc:
            # A conflict can mean another actor already settled the task, or
            # that the failure commit did not apply.  Re-read durable truth
            # before deciding whether to stop retrying this task.
            _LOGGER.warning(
                "durable scheduler failure commit did not apply",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
                exc_info=exc,
            )
            try:
                current = self._session.task(task_id)
            except KeyError:
                return True
            if current.status not in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return False
            if current.status is not TaskStatus.FAILED:
                return True
            if current.state.get("scheduler_failure") != reason:
                return True
        except Exception as exc:
            _LOGGER.exception(
                "durable scheduler failure commit raised an unexpected error",
                extra={"task_id": task_id, "error_type": type(exc).__name__},
            )
            return False
        with self._lock:
            self._blocked[task_id] = f"{phase}_failed"
        return True

    def _unclaim_if_owned(self, task_id: str, attempt_token: str) -> None:
        """Release only the claim captured by this dispatch invocation."""
        try:
            current = self._session.task(task_id)
        except KeyError:
            return
        if current.status is not TaskStatus.RUNNING or current.attempt_token != attempt_token:
            return
        try:
            self._session.unclaim_task(task_id)
        except (CommitConflict, KeyError, ValueError):
            return

    def _unclaim_current_attempt(self, task_id: str) -> None:
        """Release the attempt committed immediately before a failed read."""
        try:
            current = self._session.task(task_id)
        except KeyError:
            return
        if current.status is TaskStatus.RUNNING and current.attempt_token:
            self._unclaim_if_owned(task_id, current.attempt_token)

    def _place_ready_inputs(self) -> None:
        """Place queued conversation inputs once their active run has settled."""
        tasks = self._session.pending_tasks()
        active_by_conversation = {
            task.conversation_id
            for task in tasks
            if task.state.get("input_state") != "queued"
            and task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        }
        for submission in self._session.queued_submissions():
            if submission.mode != "write":
                continue
            content = submission.content
            display_only = isinstance(content, Mapping) and content.get("display_only") is True
            if not display_only and submission.conversation_id in active_by_conversation:
                continue
            try:
                self._session.place_submission(submission.submission_id)
            except (CommitConflict, KeyError, ValueError):
                continue
        queued_conversations = {
            submission.conversation_id
            for submission in self._session.queued_submissions()
            if submission.conversation_id not in active_by_conversation
        }
        for conversation_id in queued_conversations:
            try:
                self._session.place_boundary_inputs(
                    conversation_id,
                    at="final",
                    steering_mode=self._steering_mode,
                    follow_up_mode=self._follow_up_mode,
                )
            except (CommitConflict, KeyError, ValueError):
                continue

    def _defer_retry(self, task_id: str) -> None:
        with self._lock:
            self._scheduled.discard(task_id)
            self._pending.add(task_id)

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


__all__ = ["AbortInvocation", "DurableTaskScheduler", "SchedulerFailure", "TaskInvocation"]
