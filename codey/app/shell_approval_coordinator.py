"""Application coordination for durable shell approval projections."""

from __future__ import annotations

import threading
from typing import Protocol, cast

from codey.app.approval_registry import ApprovalRegistry
from codey.app.run_registry import RunRegistry
from codey.durable.session import DurableSession
from codey.runtime.effects.shell_approval_records import ShellApprovalFact, ShellApprovalRecordStore
from codey.storage.managed_outputs import ManagedOutputStore


class ShellApprovalContext(Protocol):
    """The concrete resources needed by shell approval coordination."""

    lock: threading.Lock
    approvals: ApprovalRegistry
    durable: DurableSession
    run_registry: RunRegistry
    managed_outputs: ManagedOutputStore | None
    shell_approval_records: ShellApprovalRecordStore


class ShellApprovalCoordinator:
    """Own the in-memory approval projection and durable approval bridge."""

    def __init__(self, context: ShellApprovalContext) -> None:
        self._context = context

    def add_pending(self, approval_id: str, pending: dict[str, object]) -> None:
        fact: ShellApprovalFact | None = None
        with self._context.lock:
            config = self._context.run_registry.api_selection_for(str(pending.get("run_id") or ""))
            if config is not None and pending.get("provider") == config.connection_id:
                pending = {**pending, "_api_selection": config}
            session_id = str(pending.get("session_id") or "")
            source_run_id = str(pending.get("run_id") or "")
            existing_approval = None
            if session_id and source_run_id:
                try:
                    existing_approval = self._context.durable.approval(approval_id)
                except (KeyError, OSError, TypeError, ValueError):
                    existing_approval = None
            if session_id and source_run_id and existing_approval is None:
                fact = ShellApprovalFact(
                    approval_id=approval_id,
                    session_id=session_id,
                    source_run_id=source_run_id,
                    kind="requested",
                    invocation_origin="native" if pending.get("call_id") else "text",
                    command_ref=str(pending.get("command_ref") or ""),
                    command_digest=str(pending.get("command_digest") or ""),
                    cwd=str(pending.get("cwd") or ""),
                )
        if fact is not None:
            self.record_fact(fact)
        with self._context.lock:
            self._context.approvals.add_shell(approval_id, pending)

    def record_fact(self, fact: ShellApprovalFact) -> bool:
        from codey.app.shell_approval_durable import record_fact

        return record_fact(self._context, fact)

    def commit_fact(self, fact: ShellApprovalFact) -> bool:
        from codey.app.shell_approval_durable import commit_fact

        return commit_fact(self._context, fact)

    def claim_locked(
        self,
        approval_id: str,
        *,
        claim_execution: bool = True,
    ) -> dict[str, object] | None:
        from codey.app.shell_approval_state import claim_pending_shell_approval_locked

        value = claim_pending_shell_approval_locked(
            self._context,
            approval_id,
            claim_execution=claim_execution,
        )
        return cast(dict[str, object] | None, value)

    def claim(
        self,
        approval_id: str,
        *,
        claim_execution: bool = True,
    ) -> dict[str, object] | None:
        from codey.app.shell_approval_state import (
            _claim_decision_fact,
            _claim_execution_fact,
            _rebuild_pending_from_fact,
            approval_record_facts,
        )

        with self._context.lock:
            pending = self._context.approvals.shell_snapshot().get(approval_id)
        fact = None
        if pending is None:
            try:
                record = self._context.durable.approval(approval_id)
            except (KeyError, OSError, TypeError, ValueError):
                record = None
            if record is not None:
                facts = approval_record_facts(record)
                fact = facts[-1] if facts else None
        if pending is None and fact is not None and fact.kind in {"requested", "approved"}:
            pending = _rebuild_pending_from_fact(self._context, fact)
        if pending is None:
            return None
        claimed = (
            _claim_execution_fact(self._context, pending, approval_id)
            if claim_execution
            else _claim_decision_fact(self._context, pending, approval_id, "denied")
        )
        if not claimed:
            return None
        with self._context.lock:
            self._context.approvals.pop_shell(approval_id)
        return cast(dict[str, object], pending)

    def resolve(self, approval_id: str) -> dict[str, object] | None:
        from codey.operations.shell_approval_receipt import resolve_shell_approval

        value = resolve_shell_approval(self._context.durable, self._context.managed_outputs, approval_id)
        return cast(dict[str, object] | None, value)

    def generation(self) -> int:
        with self._context.lock:
            return self._context.approvals.current_generation()

    def pending(self) -> dict[str, dict[str, object]]:
        with self._context.lock:
            value = self._context.approvals.shell_snapshot()
        return cast(dict[str, dict[str, object]], value)


__all__ = ["ShellApprovalContext", "ShellApprovalCoordinator"]
