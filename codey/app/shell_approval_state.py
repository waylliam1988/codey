"""Durable shell approval reconstruction and result resolution."""

from __future__ import annotations

from typing import Any


def _claim_execution_fact(ctx: Any, pending: dict[str, Any], approval_id: str) -> bool:
    records = getattr(ctx, "shell_approval_records", None)
    claim = getattr(records, "claim_execution", None)
    if not callable(claim):
        return False
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    return bool(claim(ShellApprovalFact(
        approval_id=approval_id,
        session_id=str(pending.get("session_id") or ""),
        source_run_id=str(pending.get("run_id") or ""),
        kind="execution_claimed",
        invocation_origin="native" if pending.get("call_id") else "text",
        command_ref=str(pending.get("command_ref") or ""),
        command_digest=str(pending.get("command_digest") or ""),
        cwd=str(pending.get("cwd") or "."),
    )))


def _claim_decision_fact(ctx: Any, pending: dict[str, Any], approval_id: str, kind: str) -> bool:
    records = getattr(ctx, "shell_approval_records", None)
    claim = getattr(records, "claim_decision", None)
    if not callable(claim):
        return False
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    return bool(claim(ShellApprovalFact(
        approval_id=approval_id,
        session_id=str(pending.get("session_id") or ""),
        source_run_id=str(pending.get("run_id") or ""),
        kind=kind,
        invocation_origin="native" if pending.get("call_id") else "text",
        command_ref=str(pending.get("command_ref") or ""),
        command_digest=str(pending.get("command_digest") or ""),
        cwd=str(pending.get("cwd") or "."),
    )))


def _source_api_selection(ctx: Any, session_id: str, source_run_id: str) -> Any:
    runtime_log = getattr(ctx, "runtime_log", None)
    if runtime_log is None:
        return None
    try:
        from codey.runtime.core.api_selection import ApiRunSelection
        from codey.runtime.core.operation_state import RuntimeOperationStore

        operation = RuntimeOperationStore(runtime_log).load(session_id, source_run_id)
        if operation is None or not operation.model_selection:
            return None
        return ApiRunSelection.from_payload(operation.model_selection)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _rebuild_pending_from_fact(ctx: Any, fact: Any) -> dict[str, Any] | None:
    """Reconstruct a pending approval projection from its verified artifact."""
    if getattr(ctx, "managed_outputs", None) is None:
        return None
    try:
        payload = ctx.managed_outputs.read_json_artifact({
            "session_id": fact.session_id,
            "run_id": fact.source_run_id,
            "handle": fact.command_ref,
            "sha256": fact.command_digest,
            "schema_version": 1,
        })
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("command"), str):
        return None
    from codey.agents.shell_approval import shell_command_event_fields, shell_command_payload

    command_fields = shell_command_payload(payload["command"])
    pending: dict[str, Any] = {
        "approval_id": fact.approval_id,
        "id": fact.approval_id,
        "session_id": fact.session_id,
        "run_id": fact.source_run_id,
        "command": payload["command"],
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
        "deferred_tool_calls": [
            item for item in (payload.get("deferred_tool_calls") or [])
            if isinstance(item, dict)
        ][:8],
    }
    pending["ui_event"] = {
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
        "deferred_tool_count": len(pending["deferred_tool_calls"]),
        "deferred_tool_calls": pending["deferred_tool_calls"],
    }
    if pending["call_id"]:
        pending["ui_event"]["call_id"] = pending["call_id"]
    source_selection = _source_api_selection(ctx, fact.session_id, fact.source_run_id)
    if source_selection is not None:
        pending["_api_selection"] = source_selection
    return pending


def restore_pending_shell_approvals(ctx: Any) -> int:
    """Restore untouched durable approvals into the in-memory projection."""
    restored = 0
    records = getattr(ctx, "shell_approval_records", None)
    if records is None:
        return restored
    for fact in records.pending_requested():
        pending = _rebuild_pending_from_fact(ctx, fact)
        if pending is None:
            continue
        ctx.approvals.add_shell(fact.approval_id, pending)
        restored += 1
    return restored


def claim_pending_shell_approval_locked(
    ctx: Any, approval_id: str, *, claim_execution: bool = True,
) -> dict[str, Any] | None:
    pending: dict[str, Any] | None = ctx.approvals.shell_snapshot().get(approval_id)
    if pending is None:
        # Only requests and accepted-but-unclaimed decisions can execute.
        # A claimed command without a receipt remains uncertain after restart.
        fact = ctx.shell_approval_records.latest(approval_id)
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



def resolve_shell_approval(ctx: Any, approval_id: str) -> dict[str, Any] | None:
    """Build a bounded redelivery payload from settled durable facts.

    This is read-only and is used by ``ResolveShellApproval`` task input.
    It never executes the command and refuses requested/claimed/abandoned
    facts or missing/tampered artifacts.
    """
    with ctx.lock:
        facts = ctx.shell_approval_records.read(approval_id)
    if not facts or ctx.managed_outputs is None:
        return None
    requested = next((fact for fact in facts if fact.kind == "requested"), None)
    settled = next((fact for fact in reversed(facts)
                    if fact.kind in {"execution_settled", "denied"}), None)
    if requested is None or settled is None or not settled.result_ref:
        return None
    try:
        command = ctx.managed_outputs.read_json_artifact({
            "session_id": requested.session_id,
            "run_id": requested.source_run_id,
            "handle": requested.command_ref,
            "sha256": requested.command_digest,
            "schema_version": 1,
        })
        result = ctx.managed_outputs.read_json_artifact({
            "session_id": settled.session_id,
            "run_id": settled.source_run_id,
            "handle": settled.result_ref,
            "sha256": settled.result_digest,
            "schema_version": 1,
        })
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(command, dict) or not isinstance(result, dict):
        return None
    return {
        "call_id": str(command.get("call_id") or ""),
        "command": str(command.get("command") or ""),
        "cwd": str(command.get("cwd") or "."),
        "model_text": str(result.get("output") or result.get("error") or ""),
        "ok": result.get("ok") is True and settled.kind == "execution_settled",
        "exit_code": result.get("exit_code") if type(result.get("exit_code")) is int else None,
        "error_code": str(result.get("status") or result.get("error") or ""),
        "truncated": bool(result.get("truncated")),
        "window_ref": command.get("window_ref"),
        "approval_id": approval_id,
        "source_run_id": requested.source_run_id,
    }

def expire_shell_results_locked(ctx: Any, **kwargs: Any) -> tuple[dict[str, Any], ...]:
    """Persist an abandoned verdict before removing in-memory approvals."""
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    candidates = []
    for approval_id, pending in ctx.approvals.shell_snapshot().items():
        run_id = str(pending.get("run_id") or "")
        session_id = str(pending.get("session_id") or "")
        if kwargs.get("run_id") and run_id != kwargs["run_id"]:
            continue
        if kwargs.get("exclude_run_id") and run_id == kwargs["exclude_run_id"]:
            continue
        if kwargs.get("session_id") and session_id != kwargs["session_id"]:
            continue
        candidates.append((approval_id, pending))
    try:
        for approval_id, pending in candidates:
            latest = ctx.shell_approval_records.latest(approval_id)
            if latest is not None and latest.kind == "requested":
                ctx.shell_approval_records.append(ShellApprovalFact(
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
        # A failed journal write preserves cards, but Stop must still revoke
        # already-claimed tickets even if later work clears its transient flag.
        ctx.approvals.invalidate_generation()
        raise
    events: tuple[dict[str, Any], ...] = ctx.approvals.expire_shell_results(**kwargs)
    return events
