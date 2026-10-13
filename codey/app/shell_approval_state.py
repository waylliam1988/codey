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


def claim_pending_shell_approval_locked(
    ctx: Any, approval_id: str, *, claim_execution: bool = True,
) -> dict[str, Any] | None:
    pending = ctx.approvals.pop_shell(approval_id)
    if pending is not None:
        if isinstance(pending, dict):
            claimed = {str(key): value for key, value in pending.items()}
            if not claim_execution:
                return claimed if _claim_decision_fact(ctx, claimed, approval_id, "denied") else None
            return claimed if _claim_execution_fact(ctx, claimed, approval_id) else None
        return None
    # Rebuild only an untouched request from durable facts after an
    # application restart. Any later fact is terminal or execution
    # uncertain and must fail closed rather than spawning again.
    fact = ctx.shell_approval_records.latest(approval_id)
    if fact is None or fact.kind != "requested" or ctx.managed_outputs is None:
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
    from codey.agents.shell_approval import shell_command_payload

    command_fields = shell_command_payload(payload["command"])
    pending = {
            "approval_id": approval_id,
            "session_id": fact.session_id,
            "run_id": fact.source_run_id,
            "command": payload["command"],
            "command_preview": command_fields["command"],
            "command_sha256": command_fields["command_sha256"],
            "command_chars": command_fields["command_chars"],
            "command_truncated": command_fields["command_truncated"],
            "cwd": str(payload.get("cwd") or "."),
            "project": str(payload.get("project") or ""),
            "attached_project": str(payload.get("attached_project") or payload.get("project") or ""),
            "intent": str(payload.get("intent") or "chat"),
            "provider": str(payload.get("provider") or ""),
            "continue_after": bool(payload.get("continue_after")),
            "max_turns": int(payload.get("max_turns") or 1),
            "call_id": str(payload.get("call_id") or ""),
            "window_ref": payload.get("window_ref"),
            "command_ref": fact.command_ref,
            "command_digest": fact.command_digest,
            "_approval_generation": ctx.approvals.current_generation(),
        }
    source_selection = _source_api_selection(ctx, fact.session_id, fact.source_run_id)
    if source_selection is not None:
        pending["_api_selection"] = source_selection
    if not claim_execution:
        return pending if _claim_decision_fact(ctx, pending, approval_id, "denied") else None
    return pending if _claim_execution_fact(ctx, pending, approval_id) else None



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

