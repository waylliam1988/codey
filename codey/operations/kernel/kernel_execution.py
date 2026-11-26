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

import time
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import Context, copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from codey.automation.browser_invocation import invocation_scope
from codey.durable.session import DurableSession
from codey.operations import tool_operation
from codey.operations.browser.browser_progress import browser_invocation
from codey.operations.browser.execution import browser_execution_receipt
from codey.operations.kernel.kernel_protocol import frozen_custom_executor_map, frozen_spec_map
from codey.operations.kernel_contracts import StopSignal, ToolExecutor
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_facts import bind_source_identity, project_execution_record, project_result_observation
from codey.operations.kernel_recovery import (
    _batch_aborted_results,
    _batch_recovery_failed_results,
    _check_batch_recovery,
    _guarded_slot_result,
    delivered_slot_typed,
)
from codey.operations.kernel_recovery_context import RecoveryContext
from codey.operations.kernel_result import (
    _consistent_tool_result,
    _error_result,
    policy_denial_result,
    strict_exit_code_or_none,
)
from codey.operations.kernel_tool_args import _validate_tool_args
from codey.operations.project.edit_settlement import (
    invalidate_workspace_identity,
    prepare_edit_settlement,
    recheck_unconfirmed_workspace,
)
from codey.operations.task_session import TaskSession, effect_coordinates, turn_effect_id
from codey.operations.tool_operation import ToolDelegate
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.tool_resources import tool_resources_for
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.types.json_value import JsonObject, require_json_string

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.tools import AgentToolFns
    from codey.automation.browser_session import BrowserSession
    from codey.durable import DurableStore
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.task_effects import KernelEffectSink
    from codey.research.answer_contract import AnswerScope
    from codey.research.tools import ResearchTools
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.toolchain.tool_spec import ToolSpec
    from codey.workspace.revision import WorkspaceRevisionStore

__all__ = [
    "execute_turn",
]


class SettleToolResult(Protocol):
    def __call__(
        self,
        identity: str,
        call: ToolCall,
        result: ToolResult,
        ok: bool,
        *,
        exit_code: object = None,
        committed: bool = False,
    ) -> ToolResult: ...


class ReconcileToolResult(Protocol):
    def __call__(self, identity: str, ok: bool, result: ToolResult) -> None: ...


@dataclass(frozen=True)
class ToolExecutionContext:
    session: TaskSession
    runnable: Mapping[str, ToolExecutor]
    delegate: ToolDelegate | None
    identity_ref: str
    active_turn: int
    base_index: int
    delivered: Mapping[str, ToolResult]
    intent_sink: KernelEffectSink | None
    controller_allowed: Collection[str] | None
    project_path: Path | None
    ignored_paths: tuple[str, ...]
    recovery: RecoveryContext
    revision_store: WorkspaceRevisionStore | None
    execution_evidence: ExecutionEvidence | None
    settle: SettleToolResult
    reconcile_intent_only: ReconcileToolResult
    stop_flag: StopSignal | None
    frozen_specs: dict[str, ToolSpec] | None
    durable: DurableStore | DurableSession | None
    managed_outputs: ManagedOutputStore | None
    run_id: str
    snapshot: TurnSnapshot | None = None
    permission_profile: str = "coding_writer"


def _build_delegate(
    session: TaskSession,
    project_path: Path | None,
    tool_fns: AgentToolFns | None,
    research_tools: ResearchTools | None,
    change_tracker: ChangeTracker | None = None,
    managed_outputs: ManagedOutputStore | None = None,
    session_id: str = "",
    run_id: str = "",
    permission_profile: str = "coding_writer",
    frozen_specs: Mapping[str, ToolSpec] | None = None,
    custom_executors: Mapping[str, ToolExecutor | None] | None = None,
    research_scope: AnswerScope | None = None,
    *, browser_session: BrowserSession | None = None, durable: DurableSession | None = None,
) -> ToolDelegate | None:
    """Build the production delegate; fail closed when a project needs one."""
    try:
        from codey.operations.task_execution import ExecutionDelegate
    except Exception as exc:
        if project_path is not None:
            raise RecoveryFailed(f"delegate unavailable with project path: {exc}") from exc
        return None
    try:
        delegate: ToolDelegate = ExecutionDelegate(
            session=session,
            project_path=project_path,
            tool_fns=tool_fns,
            research_tools=research_tools,
            browser_session=browser_session,
            change_tracker=change_tracker,
            managed_outputs=managed_outputs,
            session_id=session_id,
            run_id=run_ref(run_id),
            permission_profile=permission_profile,
            frozen_specs=dict(frozen_specs) if frozen_specs is not None else None,
            custom_executors=dict(custom_executors) if custom_executors is not None else None,
            research_scope=research_scope,
            durable=durable,
        )
    except Exception as exc:
        if project_path is not None:
            raise RecoveryFailed(f"delegate construction failed with project path: {exc}") from exc
        return None
    return delegate


def run_ref(run_id: object) -> str:
    return str(run_id or "")


def execute_turn(
    session: TaskSession,
    calls: list[ToolCall],
    *,
    executors: Mapping[str, ToolExecutor] | None = None,
    run_id: str = "",
    effect_scope: str = "",
    turn: int | None = None,
    tool_index_base: int = 0,
    project_path: Path | None = None,
    tool_fns: AgentToolFns | None = None,
    research_tools: ResearchTools | None = None,
    change_tracker: ChangeTracker | None = None,
    managed_outputs: ManagedOutputStore | None = None,
    session_id: str = "",
    permission_profile: str = "coding_writer",
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: KernelEffectSink | None = None,
    controller_allowed: Collection[str] | None = None,
    execution_evidence: ExecutionEvidence | None = None,
    workspace_ignored_paths: tuple[str, ...] = (),
    workspace_revision_store: WorkspaceRevisionStore | None = None,
    stop_flag: StopSignal | None = None,
    snapshot: TurnSnapshot | None = None,
    durable: DurableStore | DurableSession | None = None,
    nested: bool = False,
    browser_session: BrowserSession | None = None,
) -> list[ToolResult]:
    """Execute one turn; identity is run+turn+index with durable delivery first.

    The per-turn snapshot (policy ∩ controller) is enforced here as well as
    in parsing: research tools respect the controller, project tools never do.
    When ``snapshot`` is threaded (production), tool definitions and
    third-task executor bindings resolve from the frozen turn capture —
    never the live registry. Without it (unit injection) live lookup stays.
    """

    frozen_specs = frozen_spec_map(snapshot)
    from codey.operations.browser.browser_waits import InvalidBrowserWait, prepare_browser_waits

    active_turn, base_index = effect_coordinates(session.turn if turn is None else turn, tool_index_base)
    run_scope = f"{run_id}:{effect_scope}" if effect_scope else run_id
    try:
        calls = prepare_browser_waits(calls, specs=frozen_specs, executed=session.executed,
            observed=session._memory_results, delivered=delivered,
            run_scope=run_scope or "adhoc", turn=active_turn, index_base=base_index)
    except InvalidBrowserWait as exc:
        return [_error_result(exc.call, str(exc))]
    if snapshot is not None:
        if not snapshot.implementation_compatible:
            return [_error_result(call, "captured tool implementation cannot be verified after restart; call not executed")
                    for call in calls]
        if session.policy != snapshot.policy:
            return [_error_result(call, "turn policy differs from the captured authorization") for call in calls]
        if nested and durable is not None and (intent_sink is None or not intent_sink.parent_effect_id):
            return [_error_result(call, "nested execution requires its admitted parent effect") for call in calls]
        available = snapshot.nested_tool_names if nested else snapshot.tool_names
        for call in calls:
            if call.name not in available:
                return [_error_result(item, f"tool not available in the captured turn: {call.name}") for item in calls]
            _args, error = _validate_tool_args(call.name, call.args, frozen_specs=frozen_specs)
            if error:
                if call.name == "codemode" and frozen_specs is not None:
                    from codey.toolchain.tool_prompt import tool_repair_text

                    error = tool_repair_text(error, frozen_specs[call.name])
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
        committed: bool = False,
    ) -> ToolResult:
        # Parallel tools may finish together. Serialize only the commit view,
        # so two successful receipts cannot publish clones that erase each other.
        with session.projection_lock:
            result = bind_source_identity(session, call, result, ok=ok)
            staged = session.clone_projection()
            ledger = research_tools.ledger if research_tools is not None else None
            staged_ledger = ledger.clone() if ledger is not None else None
            # Receipt validation and projection happen before the commit barrier,
            # on disposable views. A committed result is the sole replay input.
            if identity not in session.executed and identity not in session.restored_effect_ids:
                project_result_observation(staged, call, result, ok=ok,
                    exit_code=strict_exit_code_or_none(exit_code), research_ledger=staged_ledger,
                    include_verification=not isinstance(durable, DurableSession))
            project_execution_record(staged, identity, call, result, ok=ok, exit_code=exit_code)
            if intent_sink is not None and not committed:
                # 统一 sink 协议：生产 sink 一律接收 result/exit_code，无旧签名回退
                intent_sink.settle(
                    identity,
                    ok if type(ok) is bool else False,
                    result=result,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                )
            if isinstance(durable, DurableSession) and intent_sink is not None:
                from codey.operations.project.verification_history import rebuild_verifications

                rebuild_verifications(staged, durable=durable, owner_task_id=run_ref_str,
                    store=managed_outputs, project_path=project_path, workspace_store=workspace_revision_store,
                    ignored_paths=ignores)
            # The staged projection admits successful acquisition data and
            # failed investigation records from the settled receipt alike.
            # Provider-owned mutations never enter this formal projection.
            if ledger is not None and staged_ledger is not None:
                assert research_tools is not None
                ledger.commit_projection(staged_ledger)
                research_tools.sources_read.update(
                    url for source in ledger.opened_sources for url in (source.requested_url, source.final_url)
                )
                research_tools.search_result_urls.update(
                    hit.url for search in ledger.searches for hit in search.results
                )
            session.commit_projection(staged)
            with _contextlib.suppress(Exception):
                session._memory_results[identity] = result
            return result

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
        research_scope=snapshot.research.scope if snapshot is not None else None,
        browser_session=browser_session,
        durable=durable if isinstance(durable, DurableSession) else None,
    )
    _begin_pending_intents(
        intent_sink, calls, session, delivered_map, identity_ref, active_turn, base_index,
        frozen_specs=frozen_specs, batch_scope=effect_scope,
    )
    execution_context = ToolExecutionContext(
        session=session,
        runnable=runnable,
        delegate=delegate,
        identity_ref=identity_ref,
        active_turn=active_turn,
        base_index=base_index,
        delivered=delivered_map,
        intent_sink=intent_sink,
        controller_allowed=controller_allowed,
        project_path=project_path,
        ignored_paths=ignores,
        recovery=recovery_ctx,
        revision_store=workspace_revision_store,
        execution_evidence=execution_evidence,
        settle=settle,
        reconcile_intent_only=reconcile_intent_only,
        stop_flag=stop_flag,
        frozen_specs=frozen_specs,
        durable=durable,
        managed_outputs=managed_outputs,
        run_id=run_ref_str,
        snapshot=snapshot,
        permission_profile=permission_profile,
    )
    with cancellation.linked_scope(stop_flag):
        parallel = _parallel_project_reads(execution_context, calls)
        return parallel if parallel is not None else _execute_slots(execution_context, calls)


def _turn_setup(
    session: TaskSession, calls: list[ToolCall], run_id: str, effect_scope: str,
    turn: int | None, tool_index_base: int, delivered: Mapping[str, ToolResult] | None,
    workspace_ignored_paths: tuple[str, ...], project_path: Path | None,
    workspace_revision_store: WorkspaceRevisionStore | None,
) -> tuple[int, int, str, str, dict[str, ToolResult], tuple[str, ...], RecoveryContext]:
    active_turn, base_index = effect_coordinates(session.turn if turn is None else turn, tool_index_base)
    run_ref_str = str(run_id or "")
    identity_ref = f"{run_ref_str}:{effect_scope}" if effect_scope else run_ref_str
    delivered_map = dict(delivered or {})
    try:
        ignores = tuple(workspace_ignored_paths)
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
    identity_ref: str, active_turn: int, base_index: int, project_path: Path | None,
    workspace_revision_store: WorkspaceRevisionStore | None, ignores: tuple[str, ...],
    recovery_ctx: RecoveryContext,
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
        aborted: list[ToolResult] = _batch_aborted_results(list(calls))
        return aborted
    if check.kind == "FAILED":
        failed: list[ToolResult] = _batch_recovery_failed_results(
            list(calls), check.message or "recovery check failed"
        )
        return failed
    return None


def _begin_pending_intents(
    intent_sink: KernelEffectSink | None, calls: list[ToolCall], session: TaskSession,
    delivered_map: dict[str, ToolResult], identity_ref: str, active_turn: int, base_index: int,
    *, frozen_specs: Mapping[str, ToolSpec] | None = None, batch_scope: str = "",
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
        intent_sink.begin_turn(pending_items, turn=active_turn, specs=frozen_specs, scope=batch_scope)


def _settle_delivered_slot(
    delivered_map: Mapping[str, ToolResult], identity: str, call: ToolCall,
    settle: SettleToolResult, results: list[ToolResult],
) -> bool:
    stored = delivered_map.get(identity)
    if stored is not None and stored.kind == "accepted":
        restored = _consistent_tool_result(call, stored, runtime_guard=True)
        results.append(settle(identity, call, restored, ok=restored.ok, committed=True))
        return True
    slot = delivered_slot_typed(delivered_map, identity, call)
    if slot.disposition == "NO_MATCH":
        return False
    assert slot.result is not None
    results.append(settle(identity, call, slot.result, ok=slot.result.ok))
    return True


def _reconcile_guarded_slot(
    session: TaskSession, identity: str, call: ToolCall, name: str,
    project_path: Path | None, workspace_revision_store: WorkspaceRevisionStore | None,
    ignores: tuple[str, ...], recovery_ctx: RecoveryContext,
    reconcile_intent_only: ReconcileToolResult, settle: SettleToolResult,
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
            session, identity, call, name, project_path=project_path,
            revision_store=workspace_revision_store, ignored_paths=ignores,
            recovery_ctx=recovery_ctx,
        )
    except Exception:
        try:
            prior = session.executed.get(identity)
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
            record = session.executed.get(identity)
            raw_ok = record["ok"] if record is not None else False
            orig_ok = raw_ok if type(raw_ok) is bool else False
        except Exception:
            orig_ok = False
        assert slot.result is not None
        reconcile_intent_only(identity, ok=orig_ok, result=slot.result)


def _parallel_project_reads(context: ToolExecutionContext, calls: Sequence[ToolCall]) -> list[ToolResult] | None:
    if (len(calls) < 2 or context.frozen_specs is None or context.snapshot is None or context.delegate is None
            or context.project_path is None or context.runnable):
        return None
    custom = dict(context.snapshot.custom_executors)
    identities = [turn_effect_id(context.identity_ref or "adhoc", context.active_turn, context.base_index + index)
                  for index in range(len(calls))]
    for identity, call in zip(identities, calls, strict=True):
        spec = context.frozen_specs.get(call.name)
        if (custom.get(call.name) is not None or spec is None or not spec.parallel_project_read
                or identity in context.delivered or identity in context.session.executed
                or identity in context.session._memory_results or call.args.get("run_in_background") is True):
            return None
    with ExitStack() as stack:
        try:
            stack.enter_context(context.session.tool_dispatch.admit(parallel=True))
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            return None

        def read(call: ToolCall, index: int) -> tuple[ToolResult, bool, int | None]:
            if context.stop_flag is not None and context.stop_flag.is_set():
                return policy_denial_result(call, "task stopped; tool call was not executed"), False, None
            return tool_operation.execute_tool_operation(context.session, call, delegate=context.delegate, runnable=context.runnable,
                turn=context.active_turn, tool_index=context.base_index + index, project_path=context.project_path,
                snapshot=context.snapshot)

        def read_in_context(call: ToolCall, index: int, scope: Context) -> tuple[ToolResult, bool, int | None]:
            return scope.run(read, call, index)

        pool = stack.enter_context(ThreadPoolExecutor(max_workers=min(4, len(calls))))
        slots: list[ToolResult | Future[tuple[ToolResult, bool, int | None]]] = []
        for index, (identity, call) in enumerate(zip(identities, calls, strict=True)):
            guarded = _guarded_slot_result(context.session, identity, call, call.name, context.active_turn,
                context.intent_sink, context.controller_allowed, project_path=context.project_path,
                revision_store=context.revision_store, ignored_paths=context.ignored_paths,
                recovery_ctx=context.recovery, frozen_specs=context.frozen_specs)
            slots.append(guarded if guarded is not None else pool.submit(read_in_context, call, index, copy_context()))
        results: list[ToolResult] = []
        for identity, call, slot in zip(identities, calls, slots, strict=True):
            if isinstance(slot, ToolResult):
                if identity not in context.session.executed:
                    slot = replace(slot, audit={**slot.audit, "execution_disposition": "denied_before_execution"})
                _reconcile_guarded_slot(context.session, identity, call, call.name, context.project_path,
                    context.revision_store, context.ignored_paths, context.recovery, context.reconcile_intent_only,
                    context.settle, slot)
                results.append(slot)
                continue
            try:
                outcome, ok, code = slot.result()
            except Exception as exc:
                outcome, ok, code = _error_result(call, f"{call.name} failed: {exc}"), False, None
            results.append(context.settle(identity, call, outcome, ok, exit_code=code))
        return results


def _execute_slots(  # noqa: PLR0915 - settlement order is one atomic state machine
    context: ToolExecutionContext,
    calls: Sequence[ToolCall],
) -> list[ToolResult]:
    session = context.session
    runnable = context.runnable
    delegate = context.delegate
    identity_ref = context.identity_ref
    active_turn = context.active_turn
    base_index = context.base_index
    delivered_map = context.delivered
    intent_sink = context.intent_sink
    controller_allowed = context.controller_allowed
    project_path = context.project_path
    workspace_ignored_paths = context.ignored_paths
    ignores = context.ignored_paths
    recovery_ctx = context.recovery
    workspace_revision_store = context.revision_store
    execution_evidence = context.execution_evidence
    settle = context.settle
    reconcile_intent_only = context.reconcile_intent_only
    stop_flag = context.stop_flag
    frozen_specs = context.frozen_specs
    durable = context.durable
    managed_outputs = context.managed_outputs
    run_id = context.run_id
    results: list[ToolResult] = []
    workspace_unconfirmed = False
    for offset, call in enumerate(calls or []):
        name = call.name.strip().lower()
        identity = turn_effect_id(identity_ref or "adhoc", active_turn, base_index + offset)
        with ExitStack() as admission:
            try:
                # The program container must not retain a leaf lock while its
                # children re-enter this same admission and settlement path.
                # Existing receipts need neither admission nor a new verdict,
                # including when cancellation arrives before their delivery.
                if (name not in {"codemode", "task_output", "task_cancel"} and not _automatic_command(context, call)
                        and call.args.get("run_in_background") is not True
                        and identity not in session.executed and identity not in delivered_map):
                    if frozen_specs is None:
                        from codey.toolchain.tool_spec import spec_for_tool

                        spec = spec_for_tool(name)
                    else:
                        spec = frozen_specs.get(name)
                    parallel = spec is not None and spec.parallel_safe and spec.replay_class == "safe"
                    admission.enter_context(session.tool_dispatch.admit(parallel=parallel))
                    if name in {"run", "edit", "shell"} and project_path is not None:
                        from codey.runtime.core.tool_dispatch import ToolDispatch

                        admission.enter_context(ToolDispatch.for_resource(str(project_path.resolve()).casefold()).admit(parallel=False))
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded) as exc:
                if identity not in session.executed and identity not in delivered_map:
                    blocked = _error_result(call, str(exc))
                    results.append(settle(identity, call, blocked, ok=False))
                    continue
            if workspace_unconfirmed and name in {"edit", "run", "shell"}:
                blocked = policy_denial_result(
                    call,
                    "prior edit workspace identity unconfirmed (edit was applied); "
                    "verification skipped, re-check workspace before continuing",
                )
                settle(identity, call, blocked, ok=False)
                results.append(blocked)
                continue
            if _settle_delivered_slot(delivered_map, identity, call, settle, results):
                continue
            stored = session._memory_results.get(identity)
            if stored is not None and stored.kind == "accepted":
                results.append(_consistent_tool_result(call, stored, runtime_guard=True))
                continue
            guarded = _guarded_slot_result(
                session, identity, call, name, active_turn, intent_sink, controller_allowed,
                project_path=project_path, revision_store=workspace_revision_store,
                ignored_paths=ignores, recovery_ctx=recovery_ctx,
                frozen_specs=frozen_specs,
            )
            if guarded is not None:
                if identity not in session.executed:
                    guarded = replace(guarded, audit={**guarded.audit, "execution_disposition": "denied_before_execution"})
                # Fresh guard errors settle inside the helper; already-durable
                # replays only reconcile the intent and never rewrite the receipt.
                _reconcile_guarded_slot(
                    session, identity, call, name, project_path,
                    workspace_revision_store, ignores, recovery_ctx, reconcile_intent_only,
                    settle, guarded,
                )
                results.append(guarded)
                continue
            if stop_flag is not None and stop_flag.is_set():
                blocked = policy_denial_result(call, "task stopped; tool call was not executed")
                settle(identity, call, blocked, ok=False)
                results.append(blocked)
                continue
            from codey.operations.shell_authorization import denied_shell_message

            denial = denied_shell_message(call, durable=durable, managed_outputs=managed_outputs,
                                          run_id=run_id, project=project_path, shell=session.shell)
            if denial:
                blocked = policy_denial_result(call, denial, reason_code="shell_authorization_denied")
                blocked = replace(blocked, audit={**blocked.audit, "execution_disposition": "denied_before_execution"})
                settle(identity, call, blocked, ok=False)
                results.append(blocked)
                continue
            verification_blocked = recheck_unconfirmed_workspace(session, call, recovery_ctx, execution_evidence)
            if verification_blocked is not None:
                verification_blocked = replace(verification_blocked, audit={
                    **verification_blocked.audit, "execution_disposition": "denied_before_execution",
                })
                settle(identity, call, verification_blocked, ok=False)
                results.append(verification_blocked)
                continue
            if call.args.get("run_in_background") is True or _automatic_command(context, call):
                deferred = call.args.get("run_in_background") is not True
                result = _admit_background(context, identity, call, base_index + offset, defer_result=deferred)
                results.append(settle(identity, call, result, ok=result.ok,
                    exit_code=result.audit.get("exit_code"), committed=not deferred and result.kind == "accepted"))
                continue
            spec = frozen_specs.get(name) if frozen_specs is not None else None
            if spec is not None and spec.executor == "task_control":
                from codey.operations.tool_tasks.execution import control_tool_task

                result = control_tool_task(call, durable=durable, owner_task_id=run_id, store=managed_outputs,
                                           caller_policy=session.policy)
                results.append(settle(identity, call, result, ok=result.ok))
                continue
            browser_context = browser_invocation(identity, durable=durable if isinstance(durable, DurableSession) else None,
                store=managed_outputs, session_id=durable.task(run_id).conversation_id if isinstance(durable, DurableSession) else "",
                run_id=run_id)
            browser_started = time.monotonic() if call.name.startswith("browser_") else None
            with invocation_scope(browser_context):
                result, ok, exit_code = tool_operation.execute_tool_operation(session, call, delegate=delegate, runnable=runnable,
                    turn=active_turn, tool_index=base_index + offset, project_path=project_path,
                    workspace_revision_store=workspace_revision_store, snapshot=context.snapshot,
                    workspace_ignored_paths=workspace_ignored_paths)
            if browser_started is not None:
                elapsed_ms = int((time.monotonic() - browser_started) * 1000)
                result = browser_execution_receipt(result, identity, elapsed_ms)
            edit = prepare_edit_settlement(
                session, call, result, ok, project_path=project_path, execution_evidence=execution_evidence,
                workspace_ignored_paths=workspace_ignored_paths, workspace_revision_store=workspace_revision_store,
            )
            if edit is not None:
                result, unconfirmed = edit
                settle(identity, call, result, ok=result.ok)
                if unconfirmed:
                    workspace_unconfirmed = True
                    invalidate_workspace_identity(session, execution_evidence)
                results.append(result)
                continue
            results.append(settle(identity, call, result, ok=ok, exit_code=exit_code))
    return results


def _automatic_command(context: ToolExecutionContext, call: ToolCall) -> bool:
    spec = context.frozen_specs.get(call.name) if context.frozen_specs is not None else None
    return (call.name == "run" and call.args.get("run_in_background") is not False
            and spec is not None and spec.supports_background and isinstance(context.durable, DurableSession)
            and context.intent_sink is not None and context.snapshot is not None and context.project_path is not None
            and call.name not in context.runnable and not dict(context.snapshot.custom_executors).get(call.name))


def _admit_background(context: ToolExecutionContext, identity: str, call: ToolCall, index: int,
                      *, defer_result: bool = False) -> ToolResult:
    spec = context.frozen_specs.get(call.name) if context.frozen_specs is not None else None
    if (spec is None or not spec.supports_background or context.snapshot is None
            or context.intent_sink is None or call.name in context.runnable
            or dict(context.snapshot.custom_executors).get(call.name) is not None):
        return _error_result(call, "background execution requires a captured durable production operation")
    resource_binding: JsonObject | None = None
    if spec.executor == "browser":
        from codey.durable.tool_task import tool_task_id
        from codey.operations.task_execution import ExecutionDelegate

        if not isinstance(context.delegate, ExecutionDelegate) or context.delegate.browser_session is None:
            return _error_result(call, "original browser session is unavailable")
        browser = context.delegate.browser_session
        if not isinstance(context.durable, DurableSession):
            return _error_result(call, "background browser wait requires durable execution")
        try:
            resource_binding = browser.capture_wait_binding()
            if call.name == "browser_wait" and call.args.get("condition") in {"text_changed", "controls_changed"}:
                from codey.automation.browser_document import same_document
                from codey.operations.browser.browser_waits import snapshot_wait_baseline

                if context.delegate.session is None:
                    raise ValueError("text_changed requires the original task receipts")
                _baseline, document = snapshot_wait_baseline(context.delegate.session, str(call.args.get("value") or ""),
                    require_json_string(call.args.get("condition"), "condition"))
                if not same_document(document, resource_binding["browser_document"]):
                    raise ValueError("text_changed requires the original snapshot document")
            tool_resources_for(context.durable).bind_browser(tool_task_id(identity), browser, resource_binding)
        except ValueError as exc:
            return _error_result(call, str(exc))
    elif spec.executor != "project" or context.project_path is None:
        return _error_result(call, "background operation has no supported captured executor resources")
    try:
        result = context.intent_sink.admit_tool_task(identity, call, snapshot=context.snapshot,
            project=str(context.project_path.resolve()) if context.project_path is not None else "",
            permission_profile=context.permission_profile, turn=context.active_turn, tool_index=index,
            defer_result=defer_result, resource_binding=resource_binding)
    except Exception:
        if resource_binding is not None and isinstance(context.durable, DurableSession):
            tool_resources_for(context.durable).release_browser(tool_task_id(identity))
        raise
    if defer_result:
        from codey.operations.tool_tasks.execution import COMMAND_YIELD_MS, automatic_command_result

        assert isinstance(context.durable, DurableSession)
        return automatic_command_result(context.durable, call, origin_effect_id=identity,
            owner_task_id=context.run_id, store=context.managed_outputs, wait_ms=COMMAND_YIELD_MS)
    return result
