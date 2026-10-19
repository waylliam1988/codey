"""Durable shell approval reconstruction and result resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _claim_execution_fact(ctx: Any, pending: dict[str, Any], approval_id: str) -> bool:
    durable = getattr(ctx, "durable", None)
    lookup = getattr(durable, "task", None)
    source_run_id = str(pending.get("run_id") or "")
    session_id = str(pending.get("session_id") or "")
    if durable is not None and callable(lookup) and source_run_id:
        try:
            task = lookup(source_run_id)
        except (KeyError, OSError, TypeError, ValueError):
            task = None
        if task is not None and str(getattr(task, "conversation_id", "")) == session_id:
            from codey.durable.errors import CommitConflict
            from codey.durable.model import EffectRecord
            from codey.runtime.core.operation_state import lane_for_run, operation_id_for_run
            try:
                effect = EffectRecord(
                    effect_id=f"shell-{approval_id}",
                    task_id=source_run_id,
                    category="shell",
                    replay_class="unsafe",
                    args_digest=str(pending.get("command_digest") or ""),
                    status="pending",
                    intent={
                        "approval_id": approval_id,
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
                # Audit is a post-commit projection.  It is deliberately not
                # routed back through record_fact, which would create a
                # second durable approval transition.
                try:
                    from codey.runtime.effects.shell_approval_records import ShellApprovalFact
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
                except OSError:
                    pass
                return True
            except (CommitConflict, KeyError, OSError, TypeError, ValueError):
                return False
    # A task without a durable checkpoint is not executable.  The audit
    # projection cannot mint a shell claim or act as a compatibility store.
    return False


def _claim_decision_fact(ctx: Any, pending: dict[str, Any], approval_id: str, kind: str) -> bool:
    durable = getattr(ctx, "durable", None)
    lookup = getattr(durable, "task", None)
    source_run_id = str(pending.get("run_id") or "")
    session_id = str(pending.get("session_id") or "")
    if durable is not None and callable(lookup) and source_run_id:
        try:
            task = lookup(source_run_id)
        except (KeyError, OSError, TypeError, ValueError):
            task = None
        if task is not None and str(getattr(task, "conversation_id", "")) == session_id:
            from codey.durable.errors import CommitConflict
            from codey.runtime.effects.shell_approval_records import ShellApprovalFact

            try:
                return bool(ctx.record_shell_approval_fact(ShellApprovalFact(
                    approval_id=approval_id,
                    session_id=session_id,
                    source_run_id=source_run_id,
                    kind=kind,
                    invocation_origin="native" if pending.get("call_id") else "text",
                    command_ref=str(pending.get("command_ref") or ""),
                    command_digest=str(pending.get("command_digest") or ""),
                    cwd=str(pending.get("cwd") or "."),
                )))
            except (CommitConflict, KeyError, OSError, TypeError, ValueError):
                return False
    return False


def _source_api_selection(ctx: Any, session_id: str, source_run_id: str) -> Any:
    durable = getattr(ctx, "durable", None)
    lookup = getattr(durable, "task", None)
    if durable is None or not callable(lookup):
        return None
    try:
        from codey.runtime.core.api_selection import ApiRunSelection
        checkpoint = lookup(source_run_id)
        if str(getattr(checkpoint, "conversation_id", "")) != session_id:
            return None
        snapshot = getattr(checkpoint, "provider_snapshot", {})
        selection = snapshot.get("model_selection") if isinstance(snapshot, Mapping) else None
        if not isinstance(selection, Mapping) or not selection:
            return None
        return ApiRunSelection.from_payload(dict(selection))
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
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


def approval_record_facts(record: Any) -> tuple[Any, ...]:
    """Render the durable approval state as the bounded workflow shape.

    ``ShellApprovalFact`` remains an audit/export DTO.  Production recovery
    derives this compatibility shape from the one durable ``ApprovalRecord``
    instead of reading the JSONL projection back in.
    """
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

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
    continuation_status = str(getattr(record, "continuation_status", "waiting_for_result"))
    continuation_kind = {
        "resuming": "continuation_claimed",
        "started": "continuation_started",
        "failed": "continuation_failed",
        "cancelled": "continuation_cancelled",
    }.get(continuation_status)
    if continuation_kind is not None:
        facts.append(ShellApprovalFact(
            kind=continuation_kind,
            continuation_run_id=str(getattr(record, "continuation_run_id", "") or ""),
            **base,
        ))
    return tuple(facts)


def _durable_approval_histories(ctx: Any) -> dict[str, tuple[Any, ...]]:
    durable = getattr(ctx, "durable", None)
    tasks_fn = getattr(durable, "tasks", None) or getattr(durable, "pending_tasks", None)
    if not callable(tasks_fn):
        return {}
    from codey.durable.model import ApprovalRecord

    histories: dict[str, tuple[Any, ...]] = {}
    try:
        tasks = tasks_fn()
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


def _durable_approval_facts(ctx: Any) -> tuple[Any, ...]:
    """Reconstruct approval facts from non-terminal task checkpoints."""
    rows: list[Any] = []
    for facts in _durable_approval_histories(ctx).values():
        last = facts[-1]
        if last.kind in {"requested", "approved"}:
            rows.append(last)
    return tuple(rows)


def restore_pending_shell_approvals(ctx: Any) -> int:
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
    ctx: Any, approval_id: str, *, claim_execution: bool = True,
) -> dict[str, Any] | None:
    pending: dict[str, Any] | None = ctx.approvals.shell_snapshot().get(approval_id)
    if pending is None:
        # Only requests and accepted-but-unclaimed decisions can execute.
        # A claimed command without a receipt remains uncertain after restart.
        durable = getattr(ctx, "durable", None)
        lookup = getattr(durable, "approval", None)
        if not callable(lookup):
            return None
        try:
            record = lookup(approval_id)
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



def resolve_shell_approval(ctx: Any, approval_id: str) -> dict[str, Any] | None:
    """Build a bounded redelivery payload from settled durable facts.

    This is read-only and is used by ``ResolveShellApproval`` task input.
    It never executes the command and refuses requested/claimed/abandoned
    facts or missing/tampered artifacts.
    """
    durable = getattr(ctx, "durable", None)
    lookup = getattr(durable, "approval", None)
    if not callable(lookup):
        return None
    try:
        record = lookup(approval_id)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        record = None
    facts = approval_record_facts(record) if record is not None else ()
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
            durable = getattr(ctx, "durable", None)
            lookup = getattr(durable, "approval", None)
            if not callable(lookup):
                continue
            try:
                record = lookup(approval_id)
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if str(record.status) == "requested":
                ctx.record_shell_approval_fact(ShellApprovalFact(
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
        # A failed durable verdict must leave the card visible while revoking
        # any in-flight shell ticket for this generation.
        ctx.approvals.invalidate_generation()
        raise
    events: tuple[dict[str, Any], ...] = ctx.approvals.expire_shell_results(**kwargs)
    return events
