"""Durable approval decisions and independently retryable result delivery.

A decision is committed before HTTP acknowledgement. Only an execution claim
may mint a shell ticket; a lost claimed result is never automatically rerun.
The worker owns no HTTP response or DOM lifetime.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

if TYPE_CHECKING:
    from codey.app.context import AppContext

from codey.agents.shell_approval import shell_command_event_fields
from codey.app.shell_approval_state import (
    _claim_decision_fact,
    _durable_approval_histories,
    _rebuild_pending_from_fact,
    approval_record_facts,
)
from codey.durable.model import TASK_TERMINAL_STATUSES, TaskCheckpoint, TaskStatus
from codey.runtime.effects.shell_approval_records import ShellApprovalFact

logger = logging.getLogger(__name__)
Submit = Callable[..., str | None]

ApprovalDecision = Literal['pending', 'allow', 'deny', 'cancelled']
ApprovalExecution = Literal['not_started', 'not_required', 'queued', 'running', 'settled', 'uncertain']
ApprovalContinuation = Literal[
    'waiting_for_result',
    'queued',
    'not_required',
    'resuming',
    'started',
    'failed',
    'unavailable',
    'cancelled',
]


class ApprovalStatus(TypedDict):
    type: Literal['shell_approval_status']
    id: str
    approval_id: str
    session_id: str
    run_id: str
    source_run_id: str
    decision: ApprovalDecision
    execution: ApprovalExecution
    continuation: ApprovalContinuation
    continuation_run_id: str
    execution_id: str
    revision: int


_CONTINUATION_STATES: dict[str, ApprovalContinuation] = {
    'continuation_claimed': 'resuming',
    'continuation_started': 'started',
    'continuation_failed': 'failed',
    'continuation_cancelled': 'cancelled',
}


class ShellApprovalWorkflow:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._condition = threading.Condition()
        self._workers: dict[str, threading.Thread] = {}
        self._executing: set[str] = set()
        self._execution_ids: dict[str, str] = {}
        self._closing = False

    def _pending(self, fact: ShellApprovalFact) -> dict[str, Any] | None:
        pending = _rebuild_pending_from_fact(self.ctx, fact)
        return pending if pending is None or isinstance(pending, dict) else None

    def _facts(self, approval_id: str) -> tuple[ShellApprovalFact, ...]:
        """Read the canonical durable approval state.

        The JSONL record is an optional audit export.  It is deliberately not
        consulted here, so a missing, delayed, or corrupt audit file cannot
        change permission or recovery behavior.
        """
        durable = getattr(self.ctx, "durable", None)
        lookup = getattr(durable, "approval", None)
        if not callable(lookup):
            return ()
        try:
            record = lookup(approval_id)
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            return ()
        return cast(tuple[ShellApprovalFact, ...], approval_record_facts(record))

    def status(self, approval_id: str) -> ApprovalStatus | None:
        facts = self._facts(approval_id)
        return self._status(facts) if facts else None

    def _read_result(self, fact: ShellApprovalFact) -> dict[str, Any]:
        if self.ctx.managed_outputs is None:
            raise ValueError("Approval result store unavailable")
        result = self.ctx.managed_outputs.read_json_artifact(
            {
                "session_id": fact.session_id,
                "run_id": fact.source_run_id,
                "handle": fact.result_ref,
                "sha256": fact.result_digest,
                "schema_version": 1,
            }
        )
        if not isinstance(result, dict):
            raise ValueError("Invalid persisted shell result")
        return result

    def _status(self, facts: tuple[ShellApprovalFact, ...]) -> ApprovalStatus:
        first, last = facts[0], facts[-1]
        kinds = {fact.kind for fact in facts}
        decision: ApprovalDecision = 'allow' if 'approved' in kinds else 'deny' if 'denied' in kinds else 'pending'
        pending = self._pending(first)
        settlement = next(
            (f for f in reversed(facts) if f.kind in {"execution_settled", "denied"} and f.result_ref), None
        )
        trusted_result = False
        if settlement is not None and pending is not None:
            try:
                self._read_result(settlement)
                trusted_result = True
            except (OSError, ValueError, TypeError):
                pass
        with self._condition:
            working = first.approval_id in self._executing
        execution: ApprovalExecution = (
            'not_required' if decision == 'deny' else 'queued' if decision == 'allow' else 'not_started'
        )
        if 'execution_claimed' in kinds:
            execution = 'running' if working else 'uncertain'
        if 'execution_settled' in kinds:
            execution = 'settled' if trusted_result else 'uncertain'
        continuation: ApprovalContinuation = 'waiting_for_result'
        if settlement is not None:
            continuation = (
                ('queued' if pending and pending.get('continue_after') else 'not_required')
                if trusted_result
                else 'unavailable'
            )
        lifecycle = next((f for f in reversed(facts) if f.kind in _CONTINUATION_STATES), None)
        if lifecycle is not None:
            continuation = _CONTINUATION_STATES[lifecycle.kind]
        if settlement is not None and not trusted_result:
            continuation = 'unavailable'
        if 'continuation_cancelled' in kinds:
            continuation = 'cancelled'
        if last.kind == 'abandoned':
            decision, continuation = 'cancelled', 'cancelled'
        return {
            'type': 'shell_approval_status',
            'id': first.approval_id,
            'approval_id': first.approval_id,
            'session_id': first.session_id,
            'run_id': first.source_run_id,
            'source_run_id': first.source_run_id,
            'decision': decision,
            'execution': execution,
            'continuation': continuation,
            'continuation_run_id': lifecycle.continuation_run_id if lifecycle else '',
            'execution_id': self._execution_ids.get(first.approval_id, ''),
            'revision': len(facts),
        }

    def busy_status(self, session_id: str = "") -> ApprovalStatus | None:
        with self._condition:
            active_ids = tuple(self._workers)
        for approval_id in active_ids:
            state = self.status(approval_id)
            if state and (
                state["execution"] in {"queued", "running"} or state["continuation"] in {"queued", "resuming"}
            ) and (not session_id or state.get("session_id") == session_id):
                return state
        return None

    def for_source(self, session_id: str, source_run_id: str = '') -> ApprovalStatus | None:
        rows = [facts for facts in _durable_approval_histories(self.ctx).values()
                if facts[0].session_id == session_id
                and (not source_run_id or facts[0].source_run_id == source_run_id)]
        return self._status(rows[-1]) if rows else None

    def list_statuses(self, *, session_id: str = '') -> list[ApprovalStatus]:
        histories = _durable_approval_histories(self.ctx)
        rows = [facts for facts in histories.values() if not session_id or facts[0].session_id == session_id]
        return [self._status(facts) for facts in rows[-100:]]

    def _durable_continuation(self, session_id: str, run_id: str) -> TaskCheckpoint | None:
        """Read continuation ownership from the canonical task checkpoint.

        Runtime JSONL is an audit projection.  It may be compacted, delayed,
        or absent after a restart, so it cannot decide whether a continuation
        is already running.  A missing durable task is an explicit unknown;
        callers must not infer that the task should be executed again.
        """
        durable = getattr(self.ctx, "durable", None)
        task = getattr(durable, "task", None)
        if durable is None or not callable(task) or not session_id or not run_id:
            return None
        try:
            checkpoint = task(run_id)
        except (KeyError, ValueError, TypeError):
            return None
        if str(getattr(checkpoint, "conversation_id", "")) != session_id:
            return None
        return cast(TaskCheckpoint, checkpoint)

    def _continuation_is_settled(self, session_id: str, run_id: str) -> bool:
        checkpoint = self._durable_continuation(session_id, run_id)
        if checkpoint is None:
            return False
        try:
            return TaskStatus(str(checkpoint.status)) in TASK_TERMINAL_STATUSES
        except (TypeError, ValueError):
            return False

    def start(self, _submit: Submit) -> None:
        """Recover committed jobs when the application service starts."""
        histories = list(_durable_approval_histories(self.ctx).values())
        for facts in histories:
            last = facts[-1]
            if last.kind == "continuation_started":
                # A started continuation is already owned by its durable task.
                # Missing or malformed ownership is unknown and must not be
                # converted into a second submission.
                checkpoint = self._durable_continuation(
                    last.session_id, last.continuation_run_id
                )
                if checkpoint is None or self._continuation_is_settled(
                    last.session_id, last.continuation_run_id
                ):
                    continue
                continue
            if last.kind in {
                "approved",
                "denied",
                "execution_settled",
                "continuation_claimed",
                "continuation_started",
            }:
                self._schedule(facts[0].approval_id)

    def accept(self, approval_id: str, approved: bool, _submit: Submit) -> tuple[int, dict[str, Any]]:
        with self.ctx._shell_spawn_gate:
            facts = self._facts(approval_id)
            if not facts:
                return 404, {"error": "approval not found"}
            state = self._status(facts)
            decision = "allow" if approved else "deny"
            if state["decision"] != "pending":
                if state["decision"] != decision:
                    return 409, {**state, "code": "decision_conflict", "error": "approval already decided"}
                return 202, dict(state)
            else:
                if self._closing or self.ctx.stop_flag_for(str(facts[0].source_run_id or "")).is_set():
                    return 409, {**state, "code": "stopped", "error": "stopped"}
                pending = self._pending(facts[0])
                if pending is None:
                    return 409, {**state, "code": "command_unavailable", "error": "approval command unavailable"}
                # Keep the UI projection intact until the verdict is durable.
                if not _claim_decision_fact(
                    self.ctx,
                    pending,
                    approval_id,
                    "approved" if approved else "denied",
                ):
                    return 409, {"code": "decision_conflict", "error": "approval already decided"}
                with self.ctx.lock:
                    self.ctx.approvals.pop_shell(approval_id)
        self._schedule(approval_id)
        accepted_state = self.status(approval_id)
        assert accepted_state is not None
        self.ctx.emit(dict(accepted_state))
        return 202, dict(accepted_state)

    def retry(self, approval_id: str, _submit: Submit) -> tuple[int, dict[str, Any]]:
        state = self.status(approval_id)
        if state is None:
            return 404, {"error": "approval not found"}
        if (
            state["execution"] == "uncertain"
            or state["decision"] in {"pending", "cancelled"}
            or state["continuation"] in {"unavailable", "cancelled", "not_required"}
        ):
            return 409, {**state, "error": "approval cannot resume", "code": "approval_not_resumable"}
        if state["continuation"] == "started":
            return 409, {**state, "error": "continuation already started", "code": "continuation_started"}
        self._schedule(approval_id)
        return 202, dict(self.status(approval_id) or state)

    def _schedule(self, approval_id: str) -> None:
        with self._condition:
            if self._closing or approval_id in self._workers:
                return
            worker = threading.Thread(
                target=self._work, args=(approval_id,), name=f"approval-{approval_id}", daemon=True
            )
            self._workers[approval_id] = worker
            worker.start()

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._workers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def cancel_queued(self, session_id: str = "") -> None:
        """Called under the Stop spawn gate and context lock."""
        for facts in _durable_approval_histories(self.ctx).values():
            first, last = facts[0], facts[-1]
            if session_id and first.session_id != session_id:
                continue
            if last.kind == "approved":
                self.ctx.record_shell_approval_fact(replace(first, kind="abandoned"))
            elif last.kind in {
                "denied",
                "execution_settled",
                "execution_claimed",
                "continuation_claimed",
                "continuation_failed",
            }:
                self.ctx.record_shell_approval_fact(replace(first, kind="continuation_cancelled"))

    def close(self) -> bool:
        with self._condition:
            self._closing = True
        return self.wait(2)

    def _write_result(
        self,
        fact: ShellApprovalFact,
        result: dict[str, Any],
        kind: str,
        pending: dict[str, Any],
    ) -> None:
        # Artifact persistence is mandatory: never publish an unrecoverable result.
        assert self.ctx.managed_outputs is not None
        artifact = self.ctx.managed_outputs.write_json_artifact(
            session_id=fact.session_id,
            run_id=fact.source_run_id,
            payload=result,
        )
        settled = replace(
            fact,
            kind=kind,
            result_ref=artifact.handle,
            result_digest=artifact.sha256,
        )
        tool_results: tuple[dict[str, object], ...] = ()
        call_id = str(pending.get("call_id") or "")
        if call_id:
            tool_results = ({
                "call_id": call_id,
                "content": str(result.get("output") or result.get("error") or ""),
                "is_error": not bool(result.get("ok")),
            },)
        settlement = {
            "result_ref": artifact.handle,
            "result_digest": artifact.sha256,
            "status": str(result.get("status") or "exit"),
            "ok": bool(result.get("ok")),
            "output": str(result.get("output") or result.get("error") or ""),
            "exit_code": result.get("exit_code"),
        }
        if kind == "execution_settled":
            # Receipt, effect settlement, tool result, and same-task wakeup
            # share one durable commit. The audit file remains post-commit.
            self.ctx.durable.settle_approval(
                fact.approval_id,
                settlement=settlement,
                resume=bool(pending.get("continue_after")),
                tool_results=tool_results,
            )
            try:
                self.ctx.shell_approval_records.append(settled)
            except OSError:
                logger.warning("shell approval audit projection unavailable: %s", fact.approval_id)
            return
        self.ctx.record_shell_approval_fact(settled)
        if pending.get("continue_after"):
            self.ctx.durable.settle_approval(
                fact.approval_id,
                settlement=settlement,
                resume=True,
                tool_results=tool_results,
            )

    def _work(self, approval_id: str) -> None:
        try:
            self._process(approval_id)
        except Exception:
            # A claimed execution without a receipt stays uncertain. Never replay it.
            logger.exception("Approval processing failed: %s", approval_id)
        finally:
            with self._condition:
                self._executing.discard(approval_id)
            try:
                state = self.status(approval_id)
                if state is not None:
                    self.ctx.emit(dict(state))
            finally:
                with self._condition:
                    self._workers.pop(approval_id, None)
                    self._condition.notify_all()

    def _process(self, approval_id: str) -> None:
        from codey.app import shell_service
        from codey.policies.limits import SHELL_OUTPUT_LIMIT, SHELL_TIMEOUT

        facts = self._facts(approval_id)
        first, last = facts[0], facts[-1]
        pending = self._pending(first)
        if pending is None:
            return
        if last.kind == "approved":
            if self._closing or self.ctx.shutdown_requested():
                return
            with self._condition:
                self._executing.add(approval_id)
            pending, ticket = shell_service.claim_shell_ticket(
                self.ctx,
                approval_id,
                timeout=SHELL_TIMEOUT,
                output_limit=SHELL_OUTPUT_LIMIT,
            )
            if pending is None:
                return
            if ticket is None:
                if self.ctx.shutdown_requested():
                    self._write_result(
                        first,
                        {
                            "ok": False,
                            "status": "stopped",
                            "exit_code": None,
                            "output": "Application closed before command execution.",
                        },
                        "execution_settled",
                        pending,
                    )
                else:
                    self.ctx.record_shell_approval_fact(replace(first, kind="abandoned"))
                return
            if not ticket.session_id or not ticket.run_id:
                ticket = replace(
                    ticket,
                    session_id=ticket.session_id or first.session_id,
                    run_id=ticket.run_id or first.source_run_id,
                )
            state = self.status(approval_id)
            if state:
                self.ctx.emit(dict(state))
            self._execution_ids[approval_id] = ticket.execution_id
            state = self.status(approval_id)
            if state:
                self.ctx.emit(dict(state))
            # Publish the execution identity at the process start boundary so
            # the browser can observe bounded output before settlement. The
            # later shell_result uses the same approval id and replaces this
            # in-place projection.
            self.ctx.emit_browser_event(
                {
                    "type": "shell_result",
                    "id": approval_id,
                    "session_id": first.session_id,
                    "run_id": first.source_run_id,
                    "approved": True,
                    **shell_command_event_fields(pending),
                    "cwd": pending["cwd"],
                    "status": "running",
                    "output": "",
                    "exit_code": None,
                    "execution_id": ticket.execution_id,
                }
            )
            result = shell_service.execute_shell_ticket(self.ctx, ticket)
            self._write_result(first, result, "execution_settled", pending)
            if (result.get("stopped") or result.get("status") == "stopped") and not self.ctx.shutdown_requested():
                self.ctx.record_shell_approval_fact(replace(first, kind="continuation_cancelled"))
        elif last.kind == "denied" and not last.result_ref:
            result = {"ok": False, "status": "denied", "output": "Denied by user.", "exit_code": None}
            self._write_result(first, result, "denied", pending)
        else:
            settlement = next(
                (f for f in reversed(facts) if f.kind in {"execution_settled", "denied"} and f.result_ref), None
            )
            if settlement is None:
                return
            result = self._read_result(settlement)
        approved = any(f.kind == "approved" for f in self._facts(approval_id))
        self.ctx.record_shell_result(
            {
                "type": "shell_result",
                "id": approval_id,
                "session_id": first.session_id,
                "run_id": first.source_run_id,
                "approved": approved,
                **shell_command_event_fields(pending),
                "cwd": pending["cwd"],
                "status": result.get("status", "exit"),
                "output": result.get("output") or result.get("error") or "",
                "exit_code": result.get("exit_code"),
                "ok": result.get("ok"),
                "truncated": bool(result.get("truncated")),
                "execution_id": result.get("execution_id", ""),
            }
        )
