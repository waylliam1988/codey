"""Durable shell approval reconstruction and result resolution."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, cast

from codey.app.approval_registry import ApprovalRegistry
from codey.app.shell_approval_durable import ApprovalFactContext, ApprovalFactDurable, record_fact
from codey.durable.model import ApprovalRecord, TaskCheckpoint
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.effects.shell_approval_records import ShellApprovalFact
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.app.shell_approval_coordinator import ShellApprovalContext

PendingApproval = dict[str, object]
ApprovalHistory = tuple[ShellApprovalFact, ...]


class ApprovalRecoveryDurable(ApprovalFactDurable, Protocol):
    """Durable capabilities required to rebuild approval projections."""

    def approval(self, approval_id: str) -> ApprovalRecord: ...

    def tasks(self) -> tuple[TaskCheckpoint, ...]: ...

    def task(self, task_id: str) -> TaskCheckpoint: ...


class ApprovalStateContext(ApprovalFactContext, Protocol):
    """Approval projection resources used by claim and reconstruction paths."""

    managed_outputs: ManagedOutputStore | None
    approvals: ApprovalRegistry


class ApprovalRecoveryContext(ApprovalStateContext, Protocol):
    """Explicit resources used by restart-time approval reconstruction."""

    @property
    def durable(self) -> ApprovalRecoveryDurable: ...


def _claim_execution_fact(
    ctx: ApprovalFactContext, pending: Mapping[str, object], approval_id: str,
) -> bool:
    from codey.durable.errors import CommitConflict
    from codey.durable.model import EffectRecord
    from codey.runtime.core.operation_state import lane_for_run, operation_id_for_run
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    source_run_id = str(pending.get("run_id") or "")
    session_id = str(pending.get("session_id") or "")
    if not source_run_id:
        return False
    try:
        task = ctx.durable.task(source_run_id)
        if task.conversation_id != session_id:
            return False
        effect = EffectRecord(
            effect_id=f"shell-{approval_id}",
            task_id=source_run_id,
            category="shell",
            replay_class="unsafe",
            args_digest=str(pending.get("command_digest") or ""),
            status="pending",
            intent={
                "approval_id": approval_id,
                "call_id": str(pending.get("call_id") or ""),
                "command_ref": str(pending.get("command_ref") or ""),
                "command_digest": str(pending.get("command_digest") or ""),
                "cwd": str(pending.get("cwd") or "."),
                "session_id": session_id,
                "run_id": source_run_id,
                "lane": lane_for_run(source_run_id),
                "operation_id": operation_id_for_run(source_run_id),
            },
        )
        result = ctx.durable.claim_approval_and_begin_effect(approval_id, effect)
        if result is None:
            return False
    except (CommitConflict, KeyError, OSError, TypeError, ValueError):
        return False
    # Audit follows the atomic claim; it must not perform another transition.
    with suppress(OSError):
        ctx.shell_approval_records.append(ShellApprovalFact(
            approval_id=approval_id,
            session_id=session_id,
            source_run_id=source_run_id,
            kind="execution_claimed",
            invocation_origin="native" if pending.get("call_id") else "text",
            command_ref=str(pending.get("command_ref") or ""),
            command_digest=str(pending.get("command_digest") or ""),
            cwd=str(pending.get("cwd") or "."),
        ))
    return True


def _claim_decision_fact(
    ctx: ApprovalFactContext, pending: Mapping[str, object], approval_id: str, kind: str,
) -> bool:
    from codey.durable.errors import CommitConflict
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    source_run_id = str(pending.get("run_id") or "")
    session_id = str(pending.get("session_id") or "")
    if not source_run_id:
        return False
    try:
        task = ctx.durable.task(source_run_id)
        if task.conversation_id != session_id:
            return False
        return record_fact(ctx, ShellApprovalFact(
            approval_id=approval_id,
            session_id=session_id,
            source_run_id=source_run_id,
            kind=kind,
            invocation_origin="native" if pending.get("call_id") else "text",
            command_ref=str(pending.get("command_ref") or ""),
            command_digest=str(pending.get("command_digest") or ""),
            cwd=str(pending.get("cwd") or "."),
        ))
    except (CommitConflict, KeyError, OSError, TypeError, ValueError):
        return False


def _source_api_selection(
    ctx: ApprovalStateContext, session_id: str, source_run_id: str,
) -> ApiRunSelection | None:
    try:
        checkpoint = ctx.durable.task(source_run_id)
        if checkpoint.conversation_id != session_id:
            return None
        selection = checkpoint.provider_snapshot.get("model_selection")
        if not isinstance(selection, Mapping) or not selection:
            return None
        return ApiRunSelection.from_payload(dict(selection))
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _rebuild_pending_from_fact(
    ctx: ApprovalStateContext, fact: ShellApprovalFact,
) -> PendingApproval | None:
    """Reconstruct a pending approval projection from its verified artifact."""
    managed_outputs = ctx.managed_outputs
    if managed_outputs is None:
        return None
    try:
        payload = managed_outputs.read_json_artifact({
            "session_id": fact.session_id,
            "run_id": fact.source_run_id,
            "handle": fact.command_ref,
            "sha256": fact.command_digest,
            "schema_version": 1,
        })
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("command"), str):
        return None
    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload
    from codey.policies.shell_analysis import request_analysis

    try:
        shell, analysis = request_analysis(payload)
    except ValueError:
        # Old/malformed requests lack the interpreter the operator would
        # authorize. Never fill it from the environment during recovery.
        return None

    command_fields = shell_command_payload(payload["command"])
    command = payload["command"]
    assert isinstance(command, str)
    deferred_tool_calls = [
        item for item in (payload.get("deferred_tool_calls") or [])
        if isinstance(item, dict)
    ][:8]
    pending: PendingApproval = {
        "shell": shell.to_payload(),
        "analysis": analysis.to_payload(),
        "tool_name": str(payload.get("tool_name") or "shell"),
        "tool_args": payload.get("tool_args"),
        "approval_id": fact.approval_id,
        "id": fact.approval_id,
        "session_id": fact.session_id,
        "run_id": fact.source_run_id,
        "command": command,
        "command_preview": command_fields["command"],
        "command_sha256": command_fields["command_sha256"],
        "command_chars": command_fields["command_chars"],
        "command_truncated": command_fields["command_truncated"],
        "cwd": str(payload.get("cwd") or "."),
        "project": str(payload.get("project") or ""),
        "attached_project": payload.get("attached_project") or None,
        "intent": str(payload.get("intent") or "chat"),
        "provider": str(payload.get("provider") or ""),
        "continue_after": bool(payload.get("continue_after")),
        "max_turns": int(payload.get("max_turns") or 1),
        "call_id": str(payload.get("call_id") or ""),
        "window_ref": payload.get("window_ref"),
        "provider_id": str(payload.get("provider_id") or ""),
        "turn": payload.get("turn"),
        "tool_index": payload.get("tool_index"),
        "command_ref": fact.command_ref,
        "command_digest": fact.command_digest,
        "_approval_generation": ctx.approvals.current_generation(),
        "risk_label": str(payload.get("risk_label") or "generic"),
        "risk_title": str(payload.get("risk_title") or ""),
        "risk_detail": str(payload.get("risk_detail") or ""),
        "post_approval_instructions": str(payload.get("post_approval_instructions") or ""),
        "deferred_tool_calls": deferred_tool_calls,
    }
    ui_event: dict[str, object] = {
        "shell": shell.to_payload(),
        "tool_name": pending["tool_name"],
        "type": "shell_request",
        "run_id": fact.source_run_id,
        "session_id": fact.session_id,
        "id": fact.approval_id,
        "project": pending["project"],
        "cwd": pending["cwd"],
        **shell_command_event_fields(pending),
        "risk_label": pending["risk_label"],
        "risk_title": pending["risk_title"],
        "risk_detail": pending["risk_detail"],
        "deferred_tool_count": len(deferred_tool_calls),
        "deferred_tool_calls": deferred_tool_calls,
    }
    call_id = pending["call_id"]
    if isinstance(call_id, str) and call_id:
        ui_event["call_id"] = call_id
    from codey.storage.command_permissions import project_rule_available, session_rule_available

    ui_event["session_rule_available"] = session_rule_available(command, shell=shell, analysis=analysis)
    ui_event["project_rule_available"] = project_rule_available(command, shell=shell, analysis=analysis)
    pending["ui_event"] = ui_event
    source_selection = _source_api_selection(ctx, fact.session_id, fact.source_run_id)
    if source_selection is not None:
        pending["_api_selection"] = source_selection
    return pending


def approval_record_facts(record: ApprovalRecord) -> ApprovalHistory:
    """Render the durable approval state as the bounded workflow shape.

    ``ShellApprovalFact`` remains an audit/export DTO.  Production recovery
    derives this compatibility shape from the one durable ``ApprovalRecord``
    instead of reading the JSONL projection back in.
    """
    base = dict(
        approval_id=record.approval_id,
        session_id=record.conversation_id,
        source_run_id=record.task_id,
        command_ref=record.command_ref,
        command_digest=record.command_digest,
        cwd=record.cwd,
    )
    facts: list[ShellApprovalFact] = [ShellApprovalFact(kind="requested", **base)]
    status = str(record.status)
    if status == "cancelled":
        status = "abandoned"
    # ApprovalRecord stores the current execution state, while the workflow
    # status contract still needs the decision that led to it.
    if status in {"execution_claimed", "execution_settled"}:
        facts.append(ShellApprovalFact(kind="approved", **base))
    if status != "requested":
        settlement: Mapping[str, object] = record.settlement if isinstance(record.settlement, Mapping) else {}
        facts.append(ShellApprovalFact(
            kind=status,
            result_ref=str(settlement.get("result_ref") or ""),
            result_digest=str(settlement.get("result_digest") or ""),
            **base,
        ))
    continuation_status = record.continuation_status
    continuation_kind = {
        "resuming": "continuation_claimed",
        "started": "continuation_started",
        "failed": "continuation_failed",
        "cancelled": "continuation_cancelled",
    }.get(continuation_status)
    if continuation_kind is not None:
        facts.append(ShellApprovalFact(
            kind=continuation_kind,
            continuation_run_id=record.continuation_run_id,
            **base,
        ))
    return tuple(facts)


def _durable_approval_histories(
    ctx: ApprovalRecoveryContext,
) -> dict[str, ApprovalHistory]:
    histories: dict[str, ApprovalHistory] = {}
    try:
        tasks = ctx.durable.tasks()
    except (KeyError, OSError, TypeError, ValueError):
        return {}
    for task in tasks:
        raw = task.state.get("approvals", {})
        if not isinstance(raw, Mapping):
            continue
        for payload in raw.values():
            if not isinstance(payload, Mapping):
                continue
            try:
                record = ApprovalRecord.from_payload(payload)
                facts = approval_record_facts(record)
            except (TypeError, ValueError):
                continue
            if facts:
                histories[facts[0].approval_id] = facts
    return histories


def _durable_approval_facts(ctx: ApprovalRecoveryContext) -> ApprovalHistory:
    """Reconstruct approval facts from non-terminal task checkpoints."""
    rows: list[ShellApprovalFact] = []
    for facts in _durable_approval_histories(ctx).values():
        last = facts[-1]
        if last.kind in {"requested", "approved"}:
            rows.append(last)
    return tuple(rows)


def restore_pending_shell_approvals(ctx: ApprovalRecoveryContext) -> int:
    """Restore untouched durable approvals into the in-memory projection."""
    restored = 0
    # The in-memory approval registry is a UI projection.  Rebuild it from
    # durable task state directly; the JSONL audit may be absent or compacted.
    candidates = list(_durable_approval_facts(ctx))
    for fact in candidates:
        pending = _rebuild_pending_from_fact(ctx, fact)
        if pending is None:
            continue
        ctx.approvals.add_shell(fact.approval_id, pending)
        restored += 1
    return restored


def claim_pending_shell_approval_locked(
    ctx: ApprovalStateContext, approval_id: str, *, claim_execution: bool = True,
) -> PendingApproval | None:
    pending: PendingApproval | None = ctx.approvals.shell_snapshot().get(approval_id)
    if pending is None:
        # Only requests and accepted-but-unclaimed decisions can execute.
        # A claimed command without a receipt remains uncertain after restart.
        try:
            record = ctx.durable.approval(approval_id)
        except (KeyError, OSError, TypeError, ValueError):
            return None
        facts = approval_record_facts(record)
        fact = facts[-1] if facts else None
        if fact is None or fact.kind not in {"requested", "approved"}:
            return None
        pending = _rebuild_pending_from_fact(ctx, fact)
    if pending is None:
        return None
    claimed = (_claim_execution_fact(ctx, pending, approval_id) if claim_execution
               else _claim_decision_fact(ctx, pending, approval_id, "denied"))
    if not claimed:
        return None
    # Never consume the UI projection before durable admission succeeds.
    ctx.approvals.pop_shell(approval_id)
    return pending



def expire_shell_results_locked(
    ctx: ShellApprovalContext, *, run_id: str | None = None,
    exclude_run_id: str | None = None, session_id: str | None = None,
    output: str = "Task stopped; command approval expired.",
) -> tuple[JsonObject, ...]:
    """Persist an abandoned verdict before removing in-memory approvals."""
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    candidates: list[tuple[str, dict[str, object]]] = []
    for approval_id, pending in ctx.approvals.shell_snapshot().items():
        source_run = str(pending.get("run_id") or "")
        source_session = str(pending.get("session_id") or "")
        if run_id and source_run != run_id:
            continue
        if exclude_run_id and source_run == exclude_run_id:
            continue
        if session_id and source_session != session_id:
            continue
        candidates.append((approval_id, pending))
    try:
        for approval_id, pending in candidates:
            try:
                record = ctx.durable.approval(approval_id)
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if str(record.status) == "requested":
                record_fact(ctx, ShellApprovalFact(
                    approval_id=approval_id,
                    session_id=str(pending.get("session_id") or ""),
                    source_run_id=str(pending.get("run_id") or ""),
                    kind="abandoned",
                    invocation_origin="native" if pending.get("call_id") else "text",
                    command_ref=str(pending.get("command_ref") or ""),
                    command_digest=str(pending.get("command_digest") or ""),
                    cwd=str(pending.get("cwd") or "."),
                ))
    except Exception:
        # Keep the card on durable failure, but invalidate in-flight tickets.
        ctx.approvals.invalidate_generation()
        raise
    return cast(tuple[JsonObject, ...], ctx.approvals.expire_shell_results(
        run_id=run_id or "", exclude_run_id=exclude_run_id or "", session_id=session_id or "", output=output,
    ))
