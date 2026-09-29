"""Single-turn tool execution for the task kernel (thin orchestrator).

Result normalization lives in :mod:`kernel_result`, trusted workspace
provenance in :mod:`kernel_provenance`, replay/batch guards in
:mod:`kernel_recovery`, and fact recording in :mod:`kernel_facts`. This
module only orchestrates: delegate construction, explicit-executor
dispatch, edit bump settlement, and per-turn execution. New modules must
never import ``execute_turn`` back (no cycles).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from typing import Any

# Re-export the split boundaries so existing imports keep working:
# ``from codey.operations.kernel_execution import _result_ok`` etc.
from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.kernel_protocol import _CONTROLLER_ALIASES, _policy_allows  # noqa: F401
from codey.operations.kernel_provenance import (
    _EXECUTOR_STRIPPED_AUDIT_KEYS,
    _KERNEL_WORKSPACE_ATTR,
    _copy_kernel_workspace_provenance,
    _disk_workspace_fingerprint,
    _kernel_workspace_identity_of,
    _session_workspace_identity,
    _sync_workspace_after_edit,
    _trusted_workspace_from_result,
    _with_trusted_workspace_state,
    sync_workspace_state_after_edit,
    with_trusted_workspace_state,  # noqa: F401
)
from codey.operations.kernel_recovery import (
    RecoveryFailed,  # noqa: F401
    _batch_aborted_results,
    _batch_recovery_failed_results,  # noqa: F401
    _batch_recovery_mismatch,
    _delivered_slot_result,
    _guarded_slot_result,
    _is_recovery_error_text,  # noqa: F401
    _is_recovery_failed_text,  # noqa: F401
    _is_recovery_mismatch_text,
    _is_unsafe_tool,
    _recovery_failed_result,  # noqa: F401
    _recovery_mismatch_result,
    _replay_settled_slot,
    _same_effect_call,  # noqa: F401
    _skip_unsettled,  # noqa: F401
    _verified_persisted_identity,  # noqa: F401
)
from codey.operations.kernel_result import (
    _call_args_digest,
    _consistent_tool_result,
    _error_result,
    _normalize_audit_exit_code,  # noqa: F401
    _normalize_delegate_result,
    _normalize_explicit_result,
    _result_ok,
    _strip_executor_workspace_audit,  # noqa: F401
    build_recovered_tool_result,
    result_ok,  # noqa: F401
    strict_exit_code_or_none,
)
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "_KERNEL_WORKSPACE_ATTR",
    "_EXECUTOR_STRIPPED_AUDIT_KEYS",
    "_batch_aborted_results",
    "_batch_recovery_mismatch",
    "_build_delegate",
    "_call_args_digest",
    "_consistent_tool_result",
    "_copy_kernel_workspace_provenance",
    "_delivered_slot_result",
    "_disk_workspace_fingerprint",
    "_error_result",
    "_explicit_policy_denial",
    "_guarded_slot_result",
    "_is_recovery_mismatch_text",
    "_is_unsafe_tool",
    "_kernel_workspace_identity_of",
    "_normalize_delegate_result",
    "_normalize_explicit_result",
    "_recovery_mismatch_result",
    "_replay_settled_slot",
    "_result_ok",
    "_run_via_delegate_or_fn",
    "_same_effect_call",
    "_session_workspace_identity",
    "_settle_edit_with_workspace_bump",
    "_settle_slot",
    "_skip_unsettled",
    "_strip_executor_workspace_audit",
    "_sync_workspace_after_edit",
    "_trusted_workspace_from_result",
    "_with_trusted_workspace_state",
    "build_recovered_tool_result",
    "execute_turn",
    "record_facts_for_result",
    "result_ok",
    "strict_exit_code_or_none",
    "sync_workspace_state_after_edit",
]


def _build_delegate(
    session: TaskSession,
    project_path: Any,
    tool_fns: Any,
    research_tools: Any,
    change_tracker: Any = None,
    managed_outputs: Any = None,
    session_id: str = "",
    run_id: str = "",
    permission_profile: str = "coding_writer",
) -> Any:
    """Build the production delegate; fail closed when a project needs one."""
    if project_path is None and research_tools is None:
        return None
    try:
        from codey.operations.task_execution import ExecutionDelegate
    except Exception as exc:
        if project_path is not None:
            raise RecoveryFailed(f"delegate unavailable with project path: {exc}") from exc
        return None
    try:
        return ExecutionDelegate(
            session=session,
            project_path=project_path,
            tool_fns=tool_fns,
            research_tools=research_tools,
            change_tracker=change_tracker,
            managed_outputs=managed_outputs,
            session_id=session_id,
            run_id=run_id,
            permission_profile=permission_profile,
        )
    except Exception as exc:
        if project_path is not None:
            raise RecoveryFailed(f"delegate construction failed with project path: {exc}") from exc
        return None


def _explicit_policy_denial(
    delegate: Any, call: ToolCall, *, project_path: Any = None
) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None, bool] | None:
    """Delegate guard for the explicit executor; fail-closed on outage.

    With a project path, a missing delegate means the project guard is
    unavailable: deny instead of allowing the explicit executor to bypass
    path policy. Without a project path (pure test injection), None still
    means allow.
    """
    if delegate is None:
        if project_path is not None:
            return (
                _error_result(
                    call,
                    "delegate unavailable with project path; refusing to run explicit executor",
                ),
                False, "", [], None, True,
            )
        return None
    try:
        handles = bool(delegate.handles(call.name if hasattr(call, "name") else ""))
    except Exception:
        return (
            _error_result(call, "policy check unavailable; refusing to run executor"),
            False, "", [], None, True,
        )
    if not handles:
        return None
    try:
        check = getattr(delegate, "_policy_check", None)
        if callable(check):
            denied, message, _approval = check(call)
            if denied:
                return (
                    ToolResult(call=call, model_text=f"ERROR: {message}"),
                    False, "", [], None, True,
                )
    except Exception as exc:
        # Fail closed: a policy-check outage never authorizes the
        # explicit executor. The executor is not invoked.
        return (
            _error_result(call, f"policy check unavailable; refusing to run {call.name or '?'}: {exc}"),
            False, "", [], None, True,
        )
    return None


def _run_via_delegate_or_fn(
    delegate: Any,
    runnable: Mapping[str, Any],
    session: TaskSession,
    call: ToolCall,
    name: str,
    *,
    active_turn: int,
    tool_index: int,
    project_path: Any = None,
) -> tuple[ToolResult, bool, str, list[dict[str, str]], int | None, bool]:
    """Execute via explicit executor first, delegate as fallback.

    An injected ``executors`` entry wins so tests (and custom harnesses)
    are never silently ignored when a real ``project_path`` creates a
    delegate. Production project runs pass no executors, so the delegate
    still owns real tools. Injected fakes still pass the delegate's project
    guards (path traversal, policy deny) so safety is never bypassed.
    """
    fn = runnable.get(name)
    if fn is not None:
        denial = _explicit_policy_denial(delegate, call, project_path=project_path)
        if denial is not None:
            return denial
        try:
            produced = fn(call)
        except Exception as exc:
            return _error_result(call, str(exc) or "tool failed"), False, "", [], None, True
        if isinstance(produced, ToolResult):
            result = _consistent_tool_result(call, produced)
        elif isinstance(produced, str):
            result = ToolResult(call=call, model_text=produced)
        else:
            result = ToolResult(call=call, model_text=str(produced))
        result, ok = _normalize_explicit_result(name, result)
        return result, ok, "", [], None, True
    if delegate is not None and delegate.handles(name):
        result, ok, opened, evidence, exit_code = delegate.execute(
            call,
            turn=active_turn,
            tool_index=tool_index,
        )
        result = _consistent_tool_result(call, result)
        result, ok, exit_code = _normalize_delegate_result(name, result, ok, exit_code)
        return result, ok, opened, evidence, exit_code, True
    return _error_result(call, f"unknown tool executor: {name or '?'}"), False, "", [], None, True


def execute_turn(
    session: TaskSession,
    calls: list[ToolCall],
    *,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
    run_id: object = "",
    effect_scope: str = "",
    turn: object | None = None,
    tool_index_base: object = 0,
    project_path: Any = None,
    tool_fns: Any = None,
    research_tools: Any = None,
    change_tracker: Any = None,
    managed_outputs: Any = None,
    session_id: str = "",
    permission_profile: str = "coding_writer",
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: Any = None,
    controller_allowed: Any = None,
    execution_evidence: Any = None,
    workspace_ignored_paths: Any = (),
    workspace_revision_store: Any = None,
) -> list[ToolResult]:
    """Execute one turn; identity is run+turn+index with durable delivery first.

    The per-turn snapshot (policy ∩ controller) is enforced here as well as
    in parsing: research tools respect the controller, project tools never do.
    """

    runnable = dict(executors or {})
    try:
        active_turn = int(turn) if turn is not None else int(session.turn or 0)
    except (TypeError, ValueError):
        active_turn = int(session.turn or 0)
    try:
        base_index = int(tool_index_base or 0)
    except (TypeError, ValueError):
        base_index = 0
    run_ref = str(run_id or "")
    identity_ref = f"{run_ref}:{effect_scope}" if effect_scope else run_ref
    delivered_map = dict(delivered or {})
    try:
        ignores = tuple(str(p) for p in (workspace_ignored_paths or ())) if workspace_ignored_paths else ()
    except Exception:
        ignores = ()
    # Pre-check the whole batch before begin_turn: any slot mismatch or
    # recovery error aborts the batch, preserves original receipts/
    # settlement, executes nothing. Native chains still need an error for
    # every call id in the batch.
    if calls and _batch_recovery_mismatch(
        session, list(calls), delivered_map, identity_ref or "adhoc", active_turn, base_index,
        project_path=project_path, revision_store=workspace_revision_store,
        ignored_paths=ignores,
    ):
        return _batch_aborted_results(list(calls))

    import contextlib as _contextlib

    def settle(identity: str, call: ToolCall, result: ToolResult, ok: bool) -> None:
        _settle_slot(session, identity, call, result, ok=ok)
        with _contextlib.suppress(Exception):
            session._memory_results[identity] = result
        if intent_sink is not None:
            intent_sink.settle(identity, bool(ok))

    results: list[ToolResult] = []
    delegate = _build_delegate(
        session,
        project_path,
        tool_fns,
        research_tools,
        change_tracker,
        managed_outputs,
        session_id,
        run_ref,
        permission_profile,
    )
    if intent_sink is not None and calls:
        pending_items = [
            (turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset), call, base_index + offset)
            for offset, call in enumerate(calls)
            if turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset) not in delivered_map
            and turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset) not in session.executed
        ]
        if pending_items:
            intent_sink.begin_turn(pending_items, turn=active_turn)
    workspace_unconfirmed = False
    for offset, call in enumerate(calls or []):
        name = str(getattr(call, "name", "") or "").strip().lower()
        identity = turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset)
        if workspace_unconfirmed:
            blocked = _error_result(
                call,
                "prior edit workspace identity unconfirmed (edit happened, revision not bumped); "
                "verification skipped, re-check workspace before continuing",
            )
            settle(identity, call, blocked, ok=False)
            results.append(blocked)
            continue
        delivered_hit = _delivered_slot_result(delivered_map, identity, call)
        if delivered_hit is not None:
            is_mismatch = str(getattr(delivered_hit, "model_text", "") or "").startswith("ERROR: recovery mismatch")
            is_failed = str(getattr(delivered_hit, "model_text", "") or "").startswith("ERROR: recovery failed")
            is_persisted_unverified = str(getattr(delivered_hit, "model_text", "") or "").startswith(
                "ERROR: persisted unsafe"
            )
            settle(identity, call, delivered_hit, ok=not (is_mismatch or is_failed or is_persisted_unverified))
            results.append(delivered_hit)
            continue
        guarded = _guarded_slot_result(
            session, identity, call, name, active_turn, intent_sink, controller_allowed,
            project_path=project_path, revision_store=workspace_revision_store,
            ignored_paths=ignores,
        )
        if guarded is not None:
            text = str(getattr(guarded, "model_text", "") or "")
            if text.startswith("ERROR: recovery mismatch") or text.startswith("ERROR:"):
                settle(identity, call, guarded, ok=False)
            # Settled replay successes are already stored; do not re-settle.
            results.append(guarded)
            continue
        result, ok, opened, evidence, exit_code, _handled = _run_via_delegate_or_fn(
            delegate,
            runnable,
            session,
            call,
            name,
            active_turn=active_turn,
            tool_index=base_index + offset,
            project_path=project_path,
        )
        # Single settlement per effect: edits defer settlement until the
        # authoritative bump decides the final result. Settling ok=True first
        # and then ok=False on bump failure double-settles the same effect
        # and the durable ledger raises "effect already settled".
        edit_done = _settle_edit_with_workspace_bump(
            session, identity, call, name, result, ok,
            opened=opened, evidence=evidence, exit_code=exit_code,
            project_path=project_path, execution_evidence=execution_evidence,
            workspace_ignored_paths=workspace_ignored_paths,
            workspace_revision_store=workspace_revision_store,
            settle=settle, results=results,
        )
        if edit_done is not None:
            if edit_done == "unconfirmed":
                workspace_unconfirmed = True
            continue
        settle(identity, call, result, ok=ok)
        record_facts_for_result(
            session, call, result, ok=ok, opened_url=opened, evidence_items=evidence, exit_code=exit_code
        )
        results.append(result)
    return results


def _settle_edit_with_workspace_bump(
    session: TaskSession,
    identity: str,
    call: ToolCall,
    name: str,
    result: ToolResult,
    ok: bool,
    *,
    opened: str,
    evidence: list[dict[str, str]],
    exit_code: int | None,
    project_path: Any,
    execution_evidence: Any,
    workspace_ignored_paths: Any,
    workspace_revision_store: Any,
    settle: Any,
    results: list[ToolResult],
) -> str | None:
    """Settle one edit with its authoritative bump; None means not handled."""
    if name != "edit" or not ok:
        return None
    try:
        changed = True
        if isinstance(result.audit, dict) and "changed" in result.audit:
            changed = bool(result.audit.get("changed"))
    except Exception:
        changed = True
    if not changed:
        return None
    try:
        rev, fp = sync_workspace_state_after_edit(
            session, project_path, execution_evidence,
            ignored_paths=workspace_ignored_paths,
            revision_store=workspace_revision_store,
        )
    except Exception:
        rev, fp = 0, ""
    if rev and fp:
        try:
            trusted = _with_trusted_workspace_state(result, revision=rev, fingerprint=fp)
        except (RecoveryFailed, Exception):
            # Edit happened but provenance attach failed: record the edit
            # fact, settle ONCE as error, block same-batch work.
            with contextlib.suppress(Exception):
                from codey.operations.kernel_facts import _record_edit_fact as _edit_fact

                _edit_fact(session, call.args if isinstance(call.args, dict) else {}, result)
            with contextlib.suppress(Exception):
                session.transcript_notes.append(f"edit: {str(result.model_text or '')[:500]}")
            failed = _error_result(
                call,
                "edit happened but workspace identity unconfirmed (provenance attach failed); "
                "re-check workspace before verifying, do not re-apply the edit",
            )
            settle(identity, call, failed, ok=False)
            results.append(failed)
            return "unconfirmed"
        settle(identity, call, trusted, ok=True)
        record_facts_for_result(
            session, call, trusted, ok=True, opened_url=opened,
            evidence_items=evidence, exit_code=exit_code,
        )
        results.append(trusted)
        return "trusted"
    if workspace_revision_store is not None:
        # Edit happened but identity unconfirmed: record the edit fact (file
        # did change), then settle ONCE as error and block same-batch work.
        with contextlib.suppress(Exception):
            from codey.operations.kernel_facts import _record_edit_fact as _edit_fact

            _edit_fact(session, call.args if isinstance(call.args, dict) else {}, result)
        with contextlib.suppress(Exception):
            session.transcript_notes.append(f"edit: {str(result.model_text or '')[:500]}")
        failed = _error_result(
            call,
            "edit happened but workspace identity unconfirmed (revision bump failed); "
            "re-check workspace before verifying, do not re-apply the edit",
        )
        settle(identity, call, failed, ok=False)
        results.append(failed)
        return "unconfirmed"
    return None


def _settle_slot(session: TaskSession, identity: str, call: ToolCall, result: ToolResult, *, ok: bool) -> None:
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        record: dict[str, Any] = {
            "name": str(result.call.name or ""),
            "ok": bool(ok),
            "call_id": str(result.call.call_id or getattr(call, "call_id", "") or ""),
            "excerpt": str(result.model_text or "")[:500],
            "args_digest": _call_args_digest(call),
        }
        # Minimal durable provenance for persisted replay: the trusted
        # (revision, fingerprint) is persisted alongside the excerpt so a
        # restored session can replay the same identity without a second
        # bump. Only the kernel side-channel is persisted, never raw audit.
        try:
            identity_obj = _kernel_workspace_identity_of(result)
            if identity_obj is not None and bool(ok):
                record["workspace_revision"] = int(identity_obj.revision)
                record["workspace_fingerprint"] = str(identity_obj.fingerprint)
        except Exception:
            pass
        session.executed[identity] = record
