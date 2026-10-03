"""Single-turn tool execution for the task kernel (thin orchestrator).

Result normalization lives in :mod:`kernel_result`, trusted workspace
provenance in :mod:`kernel_provenance`, replay/batch guards in
:mod:`kernel_recovery`, recovery construction in
:mod:`kernel_recovery_result`, and fact recording in :mod:`kernel_facts`.
This module only orchestrates: delegate construction, explicit-executor
dispatch, edit bump settlement, and per-turn execution. New modules must
never import ``execute_turn`` back (no cycles).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from codey.operations.kernel_errors import EffectSettlementFailed, RecoveryFailed
from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.kernel_protocol import _validate_tool_args, frozen_custom_executor_map, frozen_spec_map
from codey.operations.kernel_provenance import (
    _kernel_workspace_identity_of,
    _with_trusted_workspace_state,
    sync_workspace_state_after_edit,
)
from codey.operations.kernel_recovery import (
    _batch_aborted_results,
    _batch_recovery_failed_results,
    _check_batch_recovery,
    _guarded_slot_result,
    delivered_slot_typed,
)
from codey.operations.kernel_recovery_context import RecoveryContext
from codey.operations.kernel_result import (
    _call_args_digest,
    _consistent_tool_result,
    _error_result,
    _normalize_delegate_result,
    _normalize_explicit_result,
    strict_exit_code_or_none,
)
from codey.operations.task_session import TaskSession, effect_coordinates, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolResult

__all__ = [
    "execute_turn",
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
    frozen_specs: Any = None,
    custom_executors: Any = None,
) -> Any:
    """Build the production delegate; fail closed when a project needs one."""
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
            run_id=run_ref(run_id),
            permission_profile=permission_profile,
            frozen_specs=dict(frozen_specs) if frozen_specs is not None else None,
            custom_executors=dict(custom_executors) if custom_executors is not None else None,
        )
    except Exception as exc:
        if project_path is not None:
            raise RecoveryFailed(f"delegate construction failed with project path: {exc}") from exc
        return None


def run_ref(run_id: object) -> str:
    return str(run_id or "")


def _explicit_policy_denial(
    delegate: Any, call: ToolCall, *, project_path: Any = None
) -> tuple[ToolResult, bool, int | None] | None:
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
                False,
                None,
            )
        return None
    try:
        handles = bool(delegate.handles(call.name if hasattr(call, "name") else ""))
    except Exception:
        return (
            _error_result(call, "policy check unavailable; refusing to run executor"),
            False,
            None,
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
                    False,
                    None,
                )
    except Exception as exc:
        # Fail closed: a policy-check outage never authorizes the
        # explicit executor. The executor is not invoked.
        return (
            _error_result(call, f"policy check unavailable; refusing to run {call.name or '?'}: {exc}"),
            False,
            None,
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
) -> tuple[ToolResult, bool, int | None]:
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
            return _error_result(call, str(exc) or "tool failed"), False, None
        if isinstance(produced, ToolResult):
            result = _consistent_tool_result(call, produced)
        elif isinstance(produced, str):
            result = ToolResult(call=call, model_text=produced)
        else:
            result = ToolResult(call=call, model_text=str(produced))
        result, ok = _normalize_explicit_result(name, result)
        exit_code = strict_exit_code_or_none(result.audit.get("exit_code")) if name == "run" else None
        return result, ok, exit_code
    if delegate is not None and delegate.handles(name):
        result, ok, exit_code = delegate.execute(
            call,
            turn=active_turn,
            tool_index=tool_index,
        )
        result = _consistent_tool_result(call, result)
        result, ok, exit_code = _normalize_delegate_result(name, result, ok, exit_code)
        return result, ok, exit_code
    return _error_result(call, f"unknown tool executor: {name or '?'}"), False, None


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
    stop_flag: Any = None,
    snapshot: Any = None,
) -> list[ToolResult]:
    """Execute one turn; identity is run+turn+index with durable delivery first.

    The per-turn snapshot (policy ∩ controller) is enforced here as well as
    in parsing: research tools respect the controller, project tools never do.
    When ``snapshot`` is threaded (production), tool definitions and
    third-task executor bindings resolve from the frozen turn capture —
    never the live registry. Without it (unit injection) live lookup stays.
    """

    frozen_specs = frozen_spec_map(snapshot)
    if snapshot is not None:
        if session.policy != snapshot.policy:
            return [_error_result(call, "turn policy differs from the captured authorization") for call in calls]
        for call in calls:
            if call.name not in snapshot.tool_names:
                return [_error_result(item, f"tool not available in the captured turn: {call.name}") for item in calls]
            _args, error = _validate_tool_args(call.name, call.args, frozen_specs=frozen_specs)
            if error:
                return [_error_result(item, error) for item in calls]
    runnable = dict(executors or {})
    active_turn, base_index, run_ref_str, identity_ref, delivered_map, ignores, recovery_ctx = (
        _turn_setup(
            session, calls, run_id, effect_scope, turn, tool_index_base,
            delivered, workspace_ignored_paths, project_path, workspace_revision_store,
        )
    )
    # Tri-state pre-check before begin_turn: MISMATCH and FAILED both abort
    # without side effects and without invoking any executor.
    abort = _precheck_abort(
        session, calls, delivered_map, identity_ref, active_turn, base_index,
        project_path, workspace_revision_store, ignores, recovery_ctx,
    )
    if abort is not None:
        return abort

    import contextlib as _contextlib

    def settle(
        identity: str,
        call: ToolCall,
        result: ToolResult,
        ok: bool,
        *,
        exit_code: object = None,
    ) -> None:
        _settle_slot(session, identity, call, result, ok=ok, exit_code=exit_code)
        with _contextlib.suppress(Exception):
            session._memory_results[identity] = result
        if intent_sink is not None:
            # 统一 sink 协议：生产 sink 一律接收 result/exit_code，无旧签名回退
            intent_sink.settle(
                identity,
                ok if type(ok) is bool else False,
                result=result,
                exit_code=exit_code if isinstance(exit_code, int) else None,
            )

    def reconcile_intent_only(identity: str, ok: bool, result: ToolResult) -> None:
        # Already-durable replay: never rewrite ``session.executed``, only
        # the intent settlement so a prior ``settle`` outage cannot cause
        # a second unsafe execution.
        if intent_sink is None:
            return
        intent_sink.settle(identity, ok if type(ok) is bool else False, result=result, exit_code=None)

    delegate = _build_delegate(
        session,
        project_path,
        tool_fns,
        research_tools,
        change_tracker,
        managed_outputs,
        session_id,
        run_ref_str,
        permission_profile,
        frozen_specs=frozen_specs,
        custom_executors=frozen_custom_executor_map(snapshot),
    )
    _begin_pending_intents(intent_sink, calls, session, delivered_map, identity_ref, active_turn, base_index,
                           frozen_specs=frozen_specs)
    return _execute_slots(
        session, calls, runnable, delegate, identity_ref, active_turn, base_index,
        delivered_map, intent_sink, controller_allowed, project_path,
        workspace_ignored_paths, ignores, recovery_ctx, workspace_revision_store,
        execution_evidence, settle, reconcile_intent_only, stop_flag, frozen_specs,
    )


def _turn_setup(
    session: TaskSession, calls: list[ToolCall], run_id: object, effect_scope: str,
    turn: object | None, tool_index_base: object, delivered: Mapping[str, ToolResult] | None,
    workspace_ignored_paths: Any, project_path: Any, workspace_revision_store: Any,
) -> tuple[int, int, str, str, dict[str, ToolResult], tuple[str, ...], RecoveryContext]:
    active_turn, base_index = effect_coordinates(session.turn if turn is None else turn, tool_index_base)
    run_ref_str = str(run_id or "")
    identity_ref = f"{run_ref_str}:{effect_scope}" if effect_scope else run_ref_str
    delivered_map = dict(delivered or {})
    try:
        ignores = tuple(str(p) for p in (workspace_ignored_paths or ())) if workspace_ignored_paths else ()
    except Exception:
        ignores = ()
    recovery_ctx = RecoveryContext(
        project_path=project_path,
        revision_store=workspace_revision_store,
        ignored_paths=ignores,
    )
    return active_turn, base_index, run_ref_str, identity_ref, delivered_map, ignores, recovery_ctx


def _precheck_abort(
    session: TaskSession, calls: list[ToolCall], delivered_map: dict[str, ToolResult],
    identity_ref: str, active_turn: int, base_index: int, project_path: Any,
    workspace_revision_store: Any, ignores: tuple[str, ...], recovery_ctx: RecoveryContext,
) -> list[ToolResult] | None:
    if not calls:
        return None
    check = _check_batch_recovery(
        session, list(calls), delivered_map, identity_ref or "adhoc",
        active_turn, base_index, project_path=project_path,
        revision_store=workspace_revision_store, ignored_paths=ignores,
        recovery_ctx=recovery_ctx,
    )
    if check.kind == "MISMATCH":
        return _batch_aborted_results(list(calls))
    if check.kind == "FAILED":
        return _batch_recovery_failed_results(list(calls), check.message or "recovery check failed")
    return None


def _begin_pending_intents(
    intent_sink: Any, calls: list[ToolCall], session: TaskSession,
    delivered_map: dict[str, ToolResult], identity_ref: str, active_turn: int, base_index: int,
    *, frozen_specs: Any = None,
) -> None:
    if intent_sink is None or not calls:
        return
    pending_items = [
        (turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset), call, base_index + offset)
        for offset, call in enumerate(calls)
        if turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset) not in delivered_map
        and turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset) not in session.executed
    ]
    if pending_items:
        intent_sink.begin_turn(pending_items, turn=active_turn, specs=frozen_specs)


def _settle_delivered_slot(
    delivered_map: dict[str, ToolResult], identity: str, call: ToolCall,
    settle: Any, results: list[ToolResult],
) -> bool:
    slot = delivered_slot_typed(delivered_map, identity, call)
    if slot.disposition == "NO_MATCH":
        return False
    assert slot.result is not None
    settle(identity, call, slot.result, ok=slot.disposition == "RECOVERED")
    results.append(slot.result)
    return True


def _reconcile_guarded_slot(
    session: TaskSession, identity: str, call: ToolCall, name: str, active_turn: int,
    project_path: Any, workspace_revision_store: Any, ignores: tuple[str, ...],
    recovery_ctx: RecoveryContext, reconcile_intent_only: Any, settle: Any,
    guarded: ToolResult,
) -> None:
    """Settle or reconcile one guarded result without losing receipts.

    Fresh guard errors (policy/controller/skip with no durable receipt) are
    settled as errors so intents close. Already-durable replays never rewrite
    ``session.executed``; successful replays only reconcile the intent.
    """
    try:
        from codey.operations.kernel_recovery import replay_slot_typed as _replay_typed

        slot = _replay_typed(
            session, identity, call, name, active_turn, project_path=project_path,
            revision_store=workspace_revision_store, ignored_paths=ignores,
            recovery_ctx=recovery_ctx,
        )
    except Exception:
        try:
            prior = getattr(session, "executed", {}).get(identity)
        except Exception:
            prior = None
        if prior is None:
            settle(identity, call, guarded, ok=False)
        else:
            try:
                raw_ok = prior.get("ok", False) if isinstance(prior, dict) else False
                prior_ok = raw_ok if type(raw_ok) is bool else False
            except Exception:
                prior_ok = False
            reconcile_intent_only(identity, ok=prior_ok, result=guarded)
        return
    if slot.disposition == "NO_MATCH":
        settle(identity, call, guarded, ok=False)
        return
    if slot.disposition == "RECOVERED":
        try:
            raw_ok = (session.executed.get(identity) or {}).get("ok", False)
            orig_ok = raw_ok if type(raw_ok) is bool else False
        except Exception:
            orig_ok = False
        assert slot.result is not None
        reconcile_intent_only(identity, ok=orig_ok, result=slot.result)


def _execute_slots(
    session: TaskSession, calls: list[ToolCall], runnable: dict[str, Any], delegate: Any,
    identity_ref: str, active_turn: int, base_index: int, delivered_map: dict[str, ToolResult],
    intent_sink: Any, controller_allowed: Any, project_path: Any,
    workspace_ignored_paths: Any, ignores: tuple[str, ...], recovery_ctx: RecoveryContext,
    workspace_revision_store: Any, execution_evidence: Any, settle: Any,
    reconcile_intent_only: Any, stop_flag: Any, frozen_specs: Any,
) -> list[ToolResult]:
    results: list[ToolResult] = []
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
        if _settle_delivered_slot(delivered_map, identity, call, settle, results):
            continue
        guarded = _guarded_slot_result(
            session, identity, call, name, active_turn, intent_sink, controller_allowed,
            project_path=project_path, revision_store=workspace_revision_store,
            ignored_paths=ignores, recovery_ctx=recovery_ctx,
            frozen_specs=frozen_specs,
        )
        if guarded is not None:
            # Fresh guard errors settle inside the helper; already-durable
            # replays only reconcile the intent and never rewrite the receipt.
            _reconcile_guarded_slot(
                session, identity, call, name, active_turn, project_path,
                workspace_revision_store, ignores, recovery_ctx, reconcile_intent_only,
                settle, guarded,
            )
            results.append(guarded)
            continue
        if stop_flag is not None and stop_flag.is_set():
            blocked = _error_result(call, "task stopped; tool call was not executed")
            settle(identity, call, blocked, ok=False)
            results.append(blocked)
            continue
        result, ok, exit_code = _run_via_delegate_or_fn(
            delegate, runnable, session, call, name, active_turn=active_turn,
            tool_index=base_index + offset, project_path=project_path,
        )
        from codey.workspace.revision import WorkspaceIdentity

        if name == "run" and exit_code is not None and WorkspaceIdentity.trusted_pair(
            session.workspace_revision, session.workspace_fingerprint,
        ).trusted:
            result = _with_trusted_workspace_state(
                result, revision=session.workspace_revision, fingerprint=session.workspace_fingerprint,
            )
        # Single settlement per effect: edits defer settlement until the
        # authoritative bump decides the final result.
        edit_done = _settle_edit_with_workspace_bump(
            session, identity, call, name, result, ok,
            exit_code=exit_code, project_path=project_path,
            execution_evidence=execution_evidence,
            workspace_ignored_paths=workspace_ignored_paths,
            workspace_revision_store=workspace_revision_store,
            settle=settle, results=results,
        )
        if edit_done is not None:
            if edit_done == "unconfirmed":
                workspace_unconfirmed = True
            continue
        settle(identity, call, result, ok=ok, exit_code=exit_code)
        record_facts_for_result(session, call, result, ok=ok, exit_code=exit_code)
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
    if isinstance(result.audit, dict) and "changed" in result.audit:
        raw_changed = result.audit.get("changed")
        if type(raw_changed) is not bool:
            return _settle_unconfirmed_edit(session, identity, call, "edit result changed flag must be a boolean",
                                             execution_evidence=execution_evidence, settle=settle, results=results)
        changed = raw_changed
    else:
        changed = True
    if not changed:
        return None
    try:
        rev, fp = sync_workspace_state_after_edit(
            session,
            project_path,
            execution_evidence,
            ignored_paths=workspace_ignored_paths,
            revision_store=workspace_revision_store,
        )
    except Exception:
        rev, fp = 0, ""
    if rev and fp:
        try:
            trusted = _with_trusted_workspace_state(result, revision=rev, fingerprint=fp)
        except Exception:
            return _settle_unconfirmed_edit(session, identity, call, "provenance attach failed",
                                             execution_evidence=execution_evidence, settle=settle, results=results)
        settle(identity, call, trusted, ok=True)
        record_facts_for_result(session, call, trusted, ok=True, exit_code=exit_code)
        results.append(trusted)
        return "trusted"
    return _settle_unconfirmed_edit(session, identity, call, "revision store missing or bump failed",
                                     execution_evidence=execution_evidence, settle=settle, results=results)


def _settle_unconfirmed_edit(session: TaskSession, identity: str, call: ToolCall, reason: str,
                             *, execution_evidence: Any, settle: Any, results: list[ToolResult]) -> str:
    """Persist an uncertain mutation once and invalidate its live identity."""
    failed = _error_result(
        call,
        f"edit happened but workspace identity unconfirmed ({reason}); "
        "re-check workspace before verifying, do not re-apply the edit",
    )
    failed = ToolResult(call=call, model_text=failed.model_text, audit=failed.audit,
                        canonical={"workspace_unconfirmed": True})
    settle(identity, call, failed, ok=False)
    record_facts_for_result(session, call, failed, ok=False)
    session.set_workspace_state(0, "")
    if execution_evidence is not None:
        execution_evidence.set_workspace_state(0, "")
    results.append(failed)
    return "unconfirmed"


def _settle_slot(
    session: TaskSession,
    identity: str,
    call: ToolCall,
    result: ToolResult,
    *,
    ok: bool,
    exit_code: object = None,
) -> None:
    """Settle the durable receipt; failure raises ``EffectSettlementFailed``.

    ``session._memory_results`` stays a tolerant process-local cache (handled
    by the caller with suppress). The durable ``session.executed`` write must
    never be suppressed: an already-executed tool without a receipt would look
    never-executed on the next recovery and risk a second unsafe execution.
    """
    try:
        record: dict[str, Any] = {
            "name": str(result.call.name or ""),
            "ok": ok if type(ok) is bool else False,
            "call_id": str(result.call.call_id or getattr(call, "call_id", "") or ""),
            "excerpt": str(result.model_text or "")[:500],
            "args_digest": _call_args_digest(call),
        }
        strict_exit = strict_exit_code_or_none(exit_code)
        if strict_exit is None and isinstance(result.audit, dict):
            strict_exit = strict_exit_code_or_none(result.audit.get("exit_code"))
        if strict_exit is not None:
            record["exit_code"] = strict_exit
    except Exception as exc:
        raise EffectSettlementFailed(f"effect settlement failed for {identity}: {exc}") from exc
    # Minimal durable provenance: only the kernel side-channel is persisted,
    # never raw audit. Extraction stays tolerant (missing provenance fails
    # closed on later replay); the receipt write below is fail-hard.
    try:
        identity_obj = _kernel_workspace_identity_of(result)
        if identity_obj is not None and type(ok) is bool and ok is True:
            record["workspace_revision"] = int(identity_obj.revision)
            record["workspace_fingerprint"] = str(identity_obj.fingerprint)
    except Exception as exc:
        raise EffectSettlementFailed(f"effect settlement provenance failed for {identity}: {exc}") from exc
    try:
        session.executed[identity] = record
    except Exception as exc:
        raise EffectSettlementFailed(f"effect settlement failed for {identity}: {exc}") from exc
