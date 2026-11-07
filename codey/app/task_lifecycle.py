"""Coordinate task stop and terminal projection boundaries.

The durable scheduler, shell executor, and browser projection all observe a
run through different adapters.  This coordinator owns the application-level
ordering between those adapters while leaving durable mutation ownership in
``DurableSession`` and run record ownership in ``RunRegistry``.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Protocol, cast

from codey.app.run_registry import RunRegistry
from codey.durable.scheduler import DurableTaskScheduler
from codey.durable.session import DurableSession
from codey.types.json_value import JsonObject


class ShellApprovalExpiry(Protocol):
    """Persist and remove approvals while the caller owns both app locks."""

    def __call__(
        self,
        *,
        run_id: str | None = None,
        exclude_run_id: str | None = None,
        session_id: str | None = None,
        output: str = "Task stopped; command approval expired.",
    ) -> tuple[JsonObject, ...]: ...


class TaskLifecycleCoordinator:
    """Own stop linearization and terminal run projection ordering."""

    _TERMINAL_STOP_REASONS = frozenset({
        "done",
        "error",
        "max_turns",
        "no_progress",
        "stopped",
    })

    def __init__(
        self,
        *,
        durable: DurableSession,
        run_registry: RunRegistry,
        approval_expiry: ShellApprovalExpiry,
        shell_approval_workflow_cancel: Callable[[str], None],
        shell_spawn_gate: Callable[[], threading.Lock],
        coordination_lock: threading.Lock,
        scheduler: DurableTaskScheduler,
        emit: Callable[[JsonObject], None],
        record_shell_result: Callable[[JsonObject], None],
        flush_deferred_events: Callable[[], None],
        pause_input: Callable[[str], None],
        is_closing: Callable[[], bool],
        cancel_teach: Callable[[], object],
        invalidate_approval_generation: Callable[[], None],
    ) -> None:
        self._durable = durable
        self._run_registry = run_registry
        self._approval_expiry = approval_expiry
        self._cancel_queued = shell_approval_workflow_cancel
        self._shell_spawn_gate = shell_spawn_gate
        self._coordination_lock = coordination_lock
        self._scheduler = scheduler
        self._emit = emit
        self._record_shell_result = record_shell_result
        self._flush_deferred_events = flush_deferred_events
        self._pause_input = pause_input
        self._is_closing = is_closing
        self._cancel_teach = cancel_teach
        self._invalidate_approval_generation = invalidate_approval_generation

    def finish_run(self, run_id: str, event: JsonObject) -> bool:
        """Finish a volatile run and publish its terminal projection.

        Approval expiry remains inside the shell gate and application lock;
        publication happens after both locks are released.
        """
        from codey.app.task_lifecycle_projection import version_task_event

        versioned = cast(JsonObject, version_task_event(self._durable, event))
        payload_value = self._run_registry.finish(run_id, versioned)
        if payload_value is None:
            return False
        payload = cast(JsonObject, payload_value)
        expired: tuple[JsonObject, ...] = ()
        if str(payload.get("stop_reason") or "") in self._TERMINAL_STOP_REASONS:
            with self._shell_spawn_gate(), self._coordination_lock:
                expired = self._approval_expiry(
                    run_id=run_id,
                    output="Run ended; command approval expired.",
                )
        self._emit(payload)
        for expired_event in expired:
            self._record_shell_result(expired_event)
        self._flush_deferred_events()
        return True

    def request_stop(self, run_id: str | None = None) -> None:
        """Linearize stop, durable cancellation, and shell invalidation."""
        abort_targets: tuple[str, ...] = ()
        target_session = ""
        events: tuple[JsonObject, ...] = ()
        with self._shell_spawn_gate():
            self._run_registry.request_stop(run_id)
            target_session = self._run_registry.session_for(run_id) if run_id else ""
            if run_id and not target_session:
                with contextlib.suppress(KeyError):
                    target_session = self._durable.task(run_id).conversation_id
                if not target_session:
                    # An unknown scoped stop must not become a global cancel.
                    return
            with self._coordination_lock:
                if not self._is_closing():
                    abort_targets = (
                        (str(run_id),)
                        if run_id
                        else tuple(run.run_id for run in self._run_registry.active_runs())
                    )
                self._cancel_teach()
                if self._is_closing():
                    # Shutdown interrupts live work but preserves unanswered
                    # durable decisions for the next process.
                    self._invalidate_approval_generation()
                else:
                    events = self._approval_expiry(run_id=run_id)
                    self._cancel_queued(target_session)
        # Scheduler abort may publish durable facts and therefore must happen
        # after both application coordination locks have been released.
        for target in abort_targets:
            with contextlib.suppress(KeyError, ValueError):
                self._scheduler.abort_task(target, reason="user stopped")
        for event in events:
            self._record_shell_result(event)
        if not self._is_closing():
            if not target_session and not run_id:
                owner = self._run_registry.current()
                target_session = owner.session_id if owner is not None else ""
            self._pause_input(target_session)
        self._flush_deferred_events()

    def expire_pending_shell_approvals(self) -> None:
        """Expire all pending shell approvals under the established locks."""
        events: tuple[JsonObject, ...]
        with self._shell_spawn_gate(), self._coordination_lock:
            events = self._approval_expiry()
        for event in events:
            self._record_shell_result(event)
        self._flush_deferred_events()

    def expire_stale_shell_approvals(self, active_run_id: str) -> None:
        """Expire approval cards belonging to runs other than ``active_run_id``."""
        events: tuple[JsonObject, ...]
        with self._shell_spawn_gate(), self._coordination_lock:
            events = self._approval_expiry(
                exclude_run_id=active_run_id,
                output="A new task started; command approval expired.",
            )
        for event in events:
            self._record_shell_result(event)
        self._flush_deferred_events()

__all__ = ["ShellApprovalExpiry", "TaskLifecycleCoordinator"]
