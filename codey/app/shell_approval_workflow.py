"""Durable approval decisions and independently retryable result delivery.

A decision is committed before HTTP acknowledgement. Only an execution claim
may mint a shell ticket; a lost claimed result is never automatically rerun.
The worker owns no HTTP response or DOM lifetime.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.app.context import AppContext

from codey.agents.shell_approval import shell_command_event_fields
from codey.app.shell_approval_state import _rebuild_pending_from_fact
from codey.runtime.effects.shell_approval_records import ShellApprovalFact
from codey.task.model import ResolveShellApproval

logger = logging.getLogger(__name__)
Submit = Callable[..., str | None]


class ShellApprovalWorkflow:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._condition = threading.Condition()
        self._workers: dict[str, threading.Thread] = {}
        self._executing: set[str] = set()
        self._submit: Submit | None = None
        self._closing = False

    def _pending(self, fact: ShellApprovalFact) -> dict[str, Any] | None:
        return _rebuild_pending_from_fact(self.ctx, fact)

    def status(self, approval_id: str) -> dict[str, Any] | None:
        facts = self.ctx.shell_approval_records.read(approval_id)
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

    def _status(self, facts: tuple[ShellApprovalFact, ...]) -> dict[str, Any]:
        first, last = facts[0], facts[-1]
        kinds = {fact.kind for fact in facts}
        decision = "allow" if "approved" in kinds else "deny" if "denied" in kinds else "pending"
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
        execution = "not_required" if decision == "deny" else "queued" if decision == "allow" else "not_started"
        if "execution_claimed" in kinds:
            execution = "running" if working else "uncertain"
        if "execution_settled" in kinds:
            execution = "settled" if trusted_result else "uncertain"
        continuation = "waiting_for_result"
        if settlement is not None:
            continuation = (
                ("queued" if pending and pending.get("continue_after") else "not_required")
                if trusted_result
                else "unavailable"
            )
        lifecycle = next((f for f in reversed(facts) if f.kind.startswith("continuation_")), None)
        if lifecycle is not None:
            continuation = lifecycle.kind.removeprefix("continuation_")
            if continuation == "claimed":
                continuation = "resuming"
        if settlement is not None and not trusted_result:
            continuation = "unavailable"
        if "continuation_cancelled" in kinds:
            continuation = "cancelled"
        if last.kind == "abandoned":
            decision, continuation = "cancelled", "cancelled"
        return {
            "type": "shell_approval_status",
            "id": first.approval_id,
            "approval_id": first.approval_id,
            "session_id": first.session_id,
            "run_id": first.source_run_id,
            "source_run_id": first.source_run_id,
            "decision": decision,
            "execution": execution,
            "continuation": continuation,
            "continuation_run_id": lifecycle.continuation_run_id if lifecycle else "",
            "revision": len(facts),
        }

    def busy_status(self) -> dict[str, Any] | None:
        with self._condition:
            active_ids = tuple(self._workers)
        for approval_id in active_ids:
            state = self.status(approval_id)
            if state and (
                state["execution"] in {"queued", "running"} or state["continuation"] in {"queued", "resuming"}
            ):
                return state
        return None

    def for_source(self, session_id: str, source_run_id: str = "") -> dict[str, Any] | None:
        rows = [
            facts
            for facts in self.ctx.shell_approval_records.histories().values()
            if facts[0].session_id == session_id and (not source_run_id or facts[0].source_run_id == source_run_id)
        ]
        return self._status(rows[-1]) if rows else None

    def list_statuses(self, *, session_id: str = "") -> list[dict[str, Any]]:
        histories = self.ctx.shell_approval_records.histories()
        rows = [facts for facts in histories.values() if not session_id or facts[0].session_id == session_id]
        return [self._status(facts) for facts in rows[-100:]]

    def start(self, submit: Submit) -> None:
        """Recover committed jobs when the application service starts."""
        self._submit = submit
        for facts in self.ctx.shell_approval_records.histories().values():
            last = facts[-1]
            if (
                last.kind == "continuation_started"
                and self.ctx.runtime_operations.load(
                    last.session_id,
                    last.continuation_run_id,
                )
                is not None
            ):
                continue
            if last.kind in {
                "approved",
                "denied",
                "execution_settled",
                "continuation_claimed",
                "continuation_started",
            }:
                self._schedule(facts[0].approval_id)

    def accept(self, approval_id: str, approved: bool, submit: Submit) -> tuple[int, dict[str, Any]]:
        self._submit = submit
        with self.ctx._shell_spawn_gate, self.ctx.lock:
            facts = self.ctx.shell_approval_records.read(approval_id)
            if not facts:
                return 404, {"error": "approval not found"}
            state = self._status(facts)
            decision = "allow" if approved else "deny"
            if state["decision"] != "pending":
                if state["decision"] != decision:
                    return 409, {**state, "code": "decision_conflict", "error": "approval already decided"}
                return 202, state
            else:
                if self._closing or self.ctx.run_registry.stop_flag.is_set():
                    return 409, {**state, "code": "stopped", "error": "stopped"}
                pending = self._pending(facts[0])
                if pending is None:
                    return 409, {**state, "code": "command_unavailable", "error": "approval command unavailable"}
                # Keep the UI projection intact until the verdict is durable.
                if not self.ctx.shell_approval_records.claim_decision(
                    replace(
                        facts[0],
                        kind="approved" if approved else "denied",
                    )
                ):
                    return 409, {"code": "decision_conflict", "error": "approval already decided"}
                self.ctx.approvals.pop_shell(approval_id)
        self._schedule(approval_id)
        accepted_state = self.status(approval_id)
        assert accepted_state is not None
        self.ctx.emit(accepted_state)
        return 202, accepted_state

    def retry(self, approval_id: str, submit: Submit) -> tuple[int, dict[str, Any]]:
        self._submit = submit
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
        return 202, self.status(approval_id) or state

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

    def cancel_queued(self) -> None:
        """Called under the Stop spawn gate and context lock."""
        for facts in self.ctx.shell_approval_records.histories().values():
            first, last = facts[0], facts[-1]
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

    def _write_result(self, fact: ShellApprovalFact, result: dict[str, Any], kind: str) -> None:
        # Artifact persistence is mandatory: never publish an unrecoverable result.
        assert self.ctx.managed_outputs is not None
        artifact = self.ctx.managed_outputs.write_json_artifact(
            session_id=fact.session_id,
            run_id=fact.source_run_id,
            payload=result,
        )
        self.ctx.record_shell_approval_fact(
            replace(
                fact,
                kind=kind,
                result_ref=artifact.handle,
                result_digest=artifact.sha256,
            )
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
                    self.ctx.emit(state)
            finally:
                with self._condition:
                    self._workers.pop(approval_id, None)
                    self._condition.notify_all()

    def _process(self, approval_id: str) -> None:
        from codey.app import shell_service
        from codey.policies.limits import SHELL_OUTPUT_LIMIT, SHELL_TIMEOUT

        facts = self.ctx.shell_approval_records.read(approval_id)
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
                    )
                else:
                    self.ctx.record_shell_approval_fact(replace(first, kind="abandoned"))
                return
            state = self.status(approval_id)
            if state:
                self.ctx.emit(state)
            result = shell_service.execute_shell_ticket(self.ctx, ticket)
            self._write_result(first, result, "execution_settled")
            if (result.get("stopped") or result.get("status") == "stopped") and not self.ctx.shutdown_requested():
                self.ctx.record_shell_approval_fact(replace(first, kind="continuation_cancelled"))
        elif last.kind == "denied" and not last.result_ref:
            result = {"ok": False, "status": "denied", "output": "Denied by user.", "exit_code": None}
            self._write_result(first, result, "denied")
        else:
            settlement = next(
                (f for f in reversed(facts) if f.kind in {"execution_settled", "denied"} and f.result_ref), None
            )
            if settlement is None:
                return
            result = self._read_result(settlement)
        approved = any(f.kind == "approved" for f in self.ctx.shell_approval_records.read(approval_id))
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
            }
        )
        if pending.get("continue_after"):
            self._continue(first, pending, result)

    def _continue(self, first: ShellApprovalFact, pending: dict[str, Any], result: dict[str, Any]) -> None:
        from codey.app import shell_service

        facts = self.ctx.shell_approval_records.read(first.approval_id)
        last = facts[-1]
        if any(f.kind == "continuation_cancelled" for f in facts):
            return
        if last.kind in {"continuation_cancelled", "abandoned"}:
            return
        if self.ctx.shutdown_requested():
            return
        if self.ctx.run_registry.stop_flag.is_set():
            self.ctx.record_shell_approval_fact(replace(first, kind="continuation_cancelled"))
            return
        if last.kind in {"continuation_claimed", "continuation_started", "continuation_failed"}:
            operation = self.ctx.runtime_operations.load(first.session_id, last.continuation_run_id)
            if operation is not None:
                if last.kind != "continuation_started":
                    self.ctx.record_shell_approval_fact(replace(last, kind="continuation_started"))
                return
            continuation_id = last.continuation_run_id
        else:
            continuation_id = "run_" + uuid.uuid4().hex
        if last.kind != "continuation_claimed":
            self.ctx.record_shell_approval_fact(
                replace(first, kind="continuation_claimed", continuation_run_id=continuation_id)
            )
        plan = shell_service.build_shell_approval_continuation_plan(
            pending=pending,
            result=result,
            active_run=self.ctx.current_run(),
            shell_results=(),
        )
        assert self._submit is not None
        try:
            run_id = self._submit(
                first.session_id,
                pending["attached_project"],
                plan.continuation,
                int(pending.get("max_turns") or 8),
                True,
                plan.provider_id,
                pending["intent"],
                input=ResolveShellApproval(first.approval_id),
                previous_run_id=first.source_run_id,
                run_id=continuation_id,
                **({"model_selection": pending["_api_selection"]} if pending.get("_api_selection") is not None else {}),
            )
        except Exception:
            if not self.ctx.shutdown_requested():
                self.ctx.record_shell_approval_fact(
                    replace(first, kind="continuation_failed", continuation_run_id=continuation_id)
                )
            raise
        if run_id is None and self.ctx.shutdown_requested():
            return
        kind = (
            "continuation_started"
            if run_id
            else "continuation_cancelled"
            if self.ctx.run_registry.stop_flag.is_set()
            else "continuation_failed"
        )
        self.ctx.record_shell_approval_fact(replace(first, kind=kind, continuation_run_id=run_id or continuation_id))
