"""Task run phase helpers.

Pure orchestration pieces extracted from task_run.py so the lifecycle
orchestrator stays thin. No mode behavior lives here; each helper moves
code verbatim without changing logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from typing import Any
import uuid

from codey.agents.shell_approval import ShellApprovalRequest
from codey.ghost.work_queue import GhostWorkItem
from codey.operations.chat import run_chat_mode
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.ghost_context import ghost_continuity, ghost_directive
from codey.operations.ghost_post_turn import (
    GhostTaskPolicyDeps,
    complete_or_block_work_item,
    maybe_claim_work_item,
    maybe_route_auto,
    maybe_sync_work_queue,
    release_work_item,
    run_ghost_post_turn,
)
from codey.operations.mode_dispatch import ModeDispatchDeps, dispatch_task_mode
from codey.operations.planning_flow import PlanningFlowDeps, run_planning_readonly_mode
from codey.operations.project_completion_flow import (
    AgentAccess,
    MAX_COMPLETION_REPAIR_ROUNDS,
    PersistenceAccess,
    ProjectCompletionDeps,
    ReviewAccess,
    RuntimeAccess,
    VerificationAccess,
    handle_project_tool_event,
    record_completion_proof_trace,
    run_project_mode,
)
from codey.operations.provider_preflight import connect_provider_with_preflight
from codey.operations.research_flow import (
    ResearchFlowDeps,
    default_research_search_provider,
    record_evidence_ledger_write,
    research_queue_item_title,
    run_hybrid_mode,
    run_research_mode,
    run_research_pipeline,
)
from codey.operations.result import ModeOutcome
from codey.operations.review_flow import ReviewFlowDeps, has_reviewable_diff, run_review_mode
from codey.providers import PROVIDER_LABELS
from codey.providers.capabilities import rank_providers
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.runs.ledger import RunLedgerWriter
from codey.runs.ledger_projection import event_with_projected_receipt
from codey.runtime.core.operation_state import RuntimeOperationTransitionError
from codey.runtime.observe.events import RunEvent, render_run_event, run_event_ui_payload
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.runtime.observe.terminalizer import (
    nonnegative_event_count,
    operation_outcome_from_task_done_event,
    task_done_event,
    terminal_turns,
)
from codey.policies.shell_risk import classify_shell_risk
from codey.task.kind import startup_failover_mode, trace_mode, ui_mode
from codey.task.model import TaskSubmission
from codey.workspace.config import ProjectConfigLoadResult, preferred_provider_for
from codey.workspace.revision import INITIAL_WORKSPACE_REVISION, WorkspaceState


logger = logging.getLogger(__name__)


def record_provider_failure_event(
    append_ledger_provider_failure: Callable[[str, ProviderFailure], None],
    trace_sink: Any,
    supervisor: Any,
    self_repair: Any,
    pid: str,
    failure: ProviderFailure,
) -> None:
    """Fan out one provider failure to ledger, trace, supervisor, self-repair."""
    append_ledger_provider_failure(pid, failure)
    trace_sink.call("record_provider_failure", pid, failure)
    if supervisor is None:
        return
    health = supervisor.record_failure(pid, failure)
    if self_repair is not None:
        try:
            self_repair.maybe_enqueue(pid, failure, health)
        except Exception:
            logger.exception("self-repair enqueue failed for provider %s", pid)


@dataclass(frozen=True)
class _Reservation:
    request: TaskSubmission
    run_id: str


@dataclass(frozen=True)
class _ClaimRoute:
    request: TaskSubmission
    task_kind: str
    claimed_work_item: GhostWorkItem | None
    route_result: Any | None


def ensure_run_reserved_and_started(
    state: Any,
    request: TaskSubmission,
) -> tuple[_Reservation | None, OperationOutcome | None]:
    """Reserve the run slot and mark it started; pure orchestration, no mode logic."""
    run_id = request.run_id
    if not run_id:
        reserved = state.reserve_run(
            session_id=request.session_id,
            project=request.project,
            task=request.task,
            provider_id=request.provider_id,
        )
        if reserved is None:
            return None, None
        run_id = reserved.run_id
        request = replace(request, run_id=run_id)
    if not state.start_run(run_id):
        return None, OperationOutcome.aborted(reason="run_not_started")
    return _Reservation(request=request, run_id=run_id), None


def build_run_work(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str | None,
    provider_id: str,
    max_turns: int,
    task_kind: str,
    ignored_paths: tuple[str, ...],
    trace: Any = None,
) -> tuple[RunWork | None, WorkspaceState]:
    """Build RunWork from the current workspace state and open the operation."""
    workspace_state = _current_workspace_state(deps, project, ignored_paths)
    work = RunWork(
        recent_events=[],
        evidence=ExecutionEvidence(
            workspace_revision=workspace_state.revision,
            workspace_fingerprint=workspace_state.fingerprint,
        ),
        claimed_work_item=None,
        trace=trace,
        workspace_revision=workspace_state.revision,
        workspace_fingerprint=workspace_state.fingerprint,
    )
    started_ok = _start_run_operation(
        deps,
        work,
        session_id=session_id,
        run_id=run_id,
        project=project or "",
        provider_id=provider_id,
        turn_budget=max_turns,
        max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
        task_kind=task_kind,
    )
    if not started_ok:
        return None, workspace_state
    return work, workspace_state


def claim_or_route_ghost_work(
    ghost_deps: GhostTaskPolicyDeps,
    request: TaskSubmission,
    *,
    baseline_task_kind: str,
    run_id: str,
) -> _ClaimRoute:
    """Claim a ghost work item or auto-route; raises TaskCancelled to the caller."""
    claimed_work_item: GhostWorkItem | None = None
    task_kind = baseline_task_kind
    route_result = None
    claim_result = maybe_claim_work_item(ghost_deps, request, run_id=run_id)
    if claim_result is not None:
        claimed_work_item = claim_result.item
        task_kind = claim_result.mode or task_kind
        request = replace(
            request,
            task=claim_result.task or request.task,
            continue_task=True,
        )
    else:
        route_result = maybe_route_auto(
            ghost_deps,
            request,
            baseline_mode=baseline_task_kind,
            run_id=run_id,
        )
        if route_result is not None:
            task_kind = route_result.final_mode
    return _ClaimRoute(
        request=request,
        task_kind=task_kind,
        claimed_work_item=claimed_work_item,
        route_result=route_result,
    )


def build_hooks(
    deps: Any,
    state: Any,
    work: RunWork,
    *,
    session_id: str,
    run_id: str,
    project: str | None,
    max_turns: int,
    project_config_ignored: tuple[str, ...],
    review_log_lines: int,
    project_completion_deps: ProjectCompletionDeps,
    current_provider_id: Callable[[], str] | None = None,
) -> RunHooks:
    """Assemble RunHooks closures; behavior identical to the previous inline block."""
    logged_provider_failures: set[tuple[str, str, str, str]] = set()

    def append_ledger(action: Callable[[RunLedgerWriter], None]) -> None:
        if work.ledger is None:
            return
        try:
            action(work.ledger)
        except Exception:
            work.ledger = None

    def append_ledger_provider_failure(pid: str, failure: ProviderFailure) -> None:
        key = (
            str(pid),
            str(getattr(failure, "action", "")),
            str(getattr(failure, "kind", "")),
            str(getattr(failure, "message", "")),
        )
        if key in logged_provider_failures:
            return
        logged_provider_failures.add(key)
        append_ledger(lambda ledger: ledger.append_provider_failure(pid, failure))

    def update_checkpoint(action: Callable[[Any, Any], Any]) -> None:
        if deps.work_checkpoints is None or work.work_checkpoint is None:
            return
        try:
            work.work_checkpoint = action(deps.work_checkpoints, work.work_checkpoint)
        except (OSError, ValueError):
            pass

    def on_event(event: RunEvent) -> None:
        work.turns_observed = max(work.turns_observed, nonnegative_event_count(event.turn))
        if work.record_agent_events_in_ledger:
            append_ledger(lambda ledger: ledger.append_run_event(event))
        payload = run_event_ui_payload(run_id, session_id, event)
        if payload is not None:
            state.emit(payload)
        if event.kind == "tool_start":
            return
        if project and _workspace_edit_event(event):
            work.advance_workspace_revision(
                deps.workspace_revisions,
                project,
                ignored_paths=project_config_ignored,
            )
        work.evidence.record(event)
        message = render_run_event(event)
        work.recent_events.append(message)
        if len(work.recent_events) > review_log_lines * 2:
            del work.recent_events[:review_log_lines]
        if project and event.kind == "tool" and event.call is not None and event.outcome is not None:
            handle_project_tool_event(
                project_completion_deps,
                event=event,
                project=project,
                work=work,
                run_id=run_id,
                update_checkpoint=update_checkpoint,
            )

    def on_shell_request(approval: ShellApprovalRequest) -> None:
        if not project:
            return
        cwd_rel = approval.cwd or "."
        risk = classify_shell_risk(approval.command)
        approval_id = "shell_" + uuid.uuid4().hex[:12]
        deferred_tool_calls = [item.to_payload() for item in approval.deferred_calls]
        provider_label = current_provider_id() if current_provider_id is not None else ""
        pending = {
            "id": approval_id,
            "session_id": session_id,
            "project": project,
            "cwd": cwd_rel or ".",
            "command": approval.command,
            "risk_label": risk.label,
            "risk_title": risk.title,
            "risk_detail": risk.detail,
            "post_approval_instructions": risk.post_approval_instructions,
            "max_turns": max_turns,
            "provider": provider_label,
            "continue_after": True,
            "run_id": run_id,
            "deferred_tool_count": len(approval.deferred_calls),
            "deferred_tool_calls": deferred_tool_calls,
        }
        pending["ui_event"] = {
            "type": "shell_request",
            "run_id": run_id,
            "session_id": session_id,
            "id": approval_id,
            "project": project,
            "cwd": pending["cwd"],
            "command": approval.command,
            "risk_label": risk.label,
            "risk_title": risk.title,
            "risk_detail": risk.detail,
            "deferred_tool_count": len(approval.deferred_calls),
            "deferred_tool_calls": deferred_tool_calls,
        }
        state.add_pending_shell_approval(approval_id, pending)
        state.emit(pending["ui_event"])

    supervisor = state.providers.supervisor
    self_repair = getattr(state, "self_repair", None)

    def record_provider_failure(pid: str, failure: ProviderFailure) -> None:
        record_provider_failure_event(
            append_ledger_provider_failure,
            FailOpenPromptTrace(work.trace),
            supervisor,
            self_repair,
            pid,
            failure,
        )

    def provider_failover_order() -> tuple[str, ...]:
        loader = getattr(state, "provider_failover_order", None)
        try:
            return tuple(loader()) if loader is not None else tuple(PROVIDER_LABELS)
        except Exception:
            return tuple(PROVIDER_LABELS)

    return RunHooks(
        on_event=on_event,
        on_shell_request=on_shell_request,
        update_checkpoint=update_checkpoint,
        record_provider_failure=record_provider_failure,
        append_ledger=append_ledger,
        provider_failover_order=provider_failover_order,
        supervisor=supervisor,
        trace=work.trace,
    )


def connect_and_build_frame(
    deps: Any,
    state: Any,
    request: TaskSubmission,
    work: RunWork,
    *,
    run_id: str,
    task_kind: str,
    project: str | None,
    provider_id: str,
    trace: Any,
    trace_sink: FailOpenPromptTrace,
    hooks: RunHooks,
    project_config_result: ProjectConfigLoadResult,
    recovered_tool_outcomes: tuple = (),
    recovered_tool_result_batch_id: str = "",
) -> tuple[RunFrame, Any, str]:
    """Run provider preflight and build the RunFrame; returns (frame, provider, provider_id)."""
    supervisor = state.providers.supervisor

    def ranked_failover_order() -> tuple[str, ...]:
        mode = startup_failover_mode(task_kind)
        return rank_providers(
            hooks.provider_failover_order(),
            mode=mode,
            preferred=preferred_provider_for(project_config_result.config, mode),
        )

    preflight = connect_provider_with_preflight(
        state=state,
        run_id=run_id,
        provider_id=provider_id,
        supervisor=supervisor,
        ranked_failover_order=ranked_failover_order,
        capture_provider_failure=deps.capture_provider_failure,
        record_provider_failure=hooks.record_provider_failure,
        append_ledger=hooks.append_ledger,
        trace_sink=trace_sink,
    )
    provider = preflight.provider
    provider_id = preflight.provider_id
    conversation = state.conversation_for(request.session_id)
    conversation_plan = build_conversation_plan(
        state=state,
        session_id=request.session_id,
        provider_id=provider_id,
        provider=provider,
        conversation=conversation,
        task_kind=task_kind,
        project=project,
        task=request.task,
        continue_task=request.continue_task,
        trace=trace,
    )
    project_text = str(Path(project).expanduser().resolve()) if project else ""
    frame = RunFrame(
        request=request,
        run_id=run_id,
        task_kind=task_kind,
        provider=provider,
        provider_id=provider_id,
        project_text=project_text,
        conversation=conversation_plan.conversation,
        fresh_chat=conversation_plan.fresh_chat,
        handoff=conversation_plan.handoff,
        research_handoff=conversation_plan.research_handoff,
        prior_snapshot=conversation_plan.prior_snapshot,
        recovered_owner_prompt=conversation_plan.recovered_owner_prompt,
        provider_session_changed=conversation_plan.provider_session_changed,
        preflight_tried=preflight.tried,
        preflight_switches=preflight.switches,
        trace=trace,
        recovered_tool_outcomes=recovered_tool_outcomes,
        recovered_tool_result_batch_id=recovered_tool_result_batch_id,
    )
    return frame, provider, provider_id


def settle_cancelled_run(
    deps: Any,
    state: Any,
    work: RunWork | None,
    ghost_deps: GhostTaskPolicyDeps,
    *,
    run_id: str,
    session_id: str,
    max_turns: int,
    provider_id: str,
    task_kind: str,
    project: str | None,
    frame: RunFrame | None,
    finish_trace: Callable[[dict[str, object]], None],
    conversation: Any | None = None,
) -> OperationOutcome:
    """Settle a TaskCancelled stop; mirrors the previous inline except block."""
    state.set_provider_session(provider_id, None)
    if work is not None:
        _update_checkpoint_safely(deps, work, "stopped")
    if conversation is not None:
        try:
            conversation.update_snapshot(
                replace(conversation.snapshot, provider_id=provider_id, blocker="stopped")
            )
        except Exception:
            pass
    stopped_event = task_done_event(
        run_id=run_id,
        session_id=session_id,
        summary="",
        stop_reason="stopped",
        max_turns=max_turns,
        provider=provider_id,
        mode=ui_mode(task_kind, project),
        work=work,
    )
    if work is not None and work.ledger is not None:
        try:
            work.ledger.finish(**stopped_event)
        except Exception:
            work.ledger = None
    stopped_event = event_with_projected_receipt(
        deps.run_ledgers, stopped_event, session_id=session_id, run_id=run_id
    )
    if work is not None:
        _finish_run_operation(deps, work, stopped_event)
    finish_trace(stopped_event)
    state.finish_run(run_id, stopped_event)
    release_work_item(
        ghost_deps,
        work.claimed_work_item if work is not None else None,
        run_id=run_id,
        reason="stopped",
    )
    return operation_outcome_from_task_done_event(stopped_event)


def settle_error_run(
    deps: Any,
    state: Any,
    exc: Exception,
    work: RunWork | None,
    ghost_deps: GhostTaskPolicyDeps,
    *,
    run_id: str,
    session_id: str,
    max_turns: int,
    provider_id: str,
    task_kind: str,
    project: str | None,
    frame: RunFrame | None,
    provider: Any | None,
    finish_trace: Callable[[dict[str, object]], None],
    conversation: Any | None = None,
) -> OperationOutcome:
    """Settle an unexpected error; mirrors the previous inline except block."""
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryError

    if work is not None:
        _update_checkpoint_safely(deps, work, "error")
    if conversation is not None:
        try:
            conversation.update_snapshot(
                replace(conversation.snapshot, provider_id=provider_id, blocker=str(exc))
            )
        except Exception:
            pass
    failure = None
    if not isinstance(exc, ToolResultDeliveryError):
        failure = (
            exc.failure
            if isinstance(exc, ProviderActionError)
            else deps.capture_provider_failure(
                model=PROVIDER_LABELS.get(provider_id, provider_id),
                action="task" if provider is not None else "connect",
                page=None,
                error=exc,
            )
        )
    state.run_registry.set_last_provider_failure(failure)
    if failure is not None and work is not None and work.ledger is not None:
        try:
            work.ledger.append_provider_failure(provider_id, failure)
        except Exception:
            assert work is not None
            work.ledger = None
    error_event = task_done_event(
        run_id=run_id,
        session_id=session_id,
        summary=f"ERROR: {exc}",
        stop_reason="error",
        max_turns=max_turns,
        provider=provider_id,
        mode=ui_mode(task_kind, project),
        work=work,
        provider_failure=failure.to_dict() if failure else None,
    )
    if work is not None and work.ledger is not None:
        try:
            work.ledger.finish(**error_event)
        except Exception:
            work.ledger = None
    error_event = event_with_projected_receipt(
        deps.run_ledgers, error_event, session_id=session_id, run_id=run_id
    )
    if work is not None:
        _finish_run_operation(deps, work, error_event)
    finish_trace(error_event)
    state.finish_run(run_id, error_event)
    current_work_item = work.claimed_work_item if work is not None else None
    if frame is not None:
        complete_or_block_work_item(ghost_deps, frame, error_event, current_work_item)
        maybe_sync_work_queue(ghost_deps, frame, error_event)
    else:
        run_ghost_post_turn(
            ghost_deps, None, error_event, current_work_item, project_text=str(project or "")
        )
    return operation_outcome_from_task_done_event(error_event)


def finish_mode_outcome(
    deps: Any,
    ghost_deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    work: RunWork,
    outcome: ModeOutcome,
    *,
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    finish_trace: Callable[[dict[str, object]], None],
) -> OperationOutcome:
    """Public orchestrator for mode finish; delegates to the durable helper."""
    return _finish_mode_outcome(
        deps, ghost_deps, frame, work, outcome,
        append_ledger=append_ledger, finish_trace=finish_trace,
    )

def _open_trace(
    deps: Any,
    session_id: str,
    run_id: str,
    project: str | None,
    task_kind: str,
    provider_id: str,
):
    if deps.run_traces is None:
        return None
    try:
        return deps.run_traces.open(
            run_id=run_id,
            session_id=session_id,
            project=project,
            mode_initial=trace_mode(task_kind, project),
            provider_initial=provider_id,
        )
    except Exception:
        return None


def _current_workspace_state(
    deps: Any,
    project: str | None,
    ignored_paths: tuple[str, ...],
) -> WorkspaceState:
    store = deps.workspace_revisions
    if not project:
        return WorkspaceState(INITIAL_WORKSPACE_REVISION, "")
    return store.current_state(project, ignored_paths=ignored_paths)


def _workspace_edit_event(event: RunEvent) -> bool:
    return (
        event.kind == "tool"
        and event.call is not None
        and event.outcome is not None
        and event.call.name == "edit"
        and bool(event.outcome.ok)
        and bool(event.outcome.changed)
    )


def _review_deps(deps: Any) -> ReviewFlowDeps:
    return ReviewFlowDeps(
        state=deps.state,
        collect_changes=deps.collect_changes,
        run_review=deps.run_review,
        is_git_repository=deps.is_git_repository or (lambda _project: False),
        review_log_lines=deps.review_log_lines,
    )


def _research_deps(deps: Any) -> ResearchFlowDeps:
    return ResearchFlowDeps(
        state=deps.state,
        knowledge_store=deps.knowledge_store,
        evidence_ledgers=deps.evidence_ledgers,
        search_factory=deps.search_factory or default_research_search_provider,
        run_research_advisors=deps.run_research_advisors,
        ghost_continuity=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
    )


def _planning_deps(deps: Any) -> PlanningFlowDeps:
    return PlanningFlowDeps(
        state=deps.state,
        agent_run=deps.agent_run,
        project_facts=deps.project_facts,
        knowledge_store=deps.knowledge_store,
        review_log_lines=deps.review_log_lines,
        ghost_directive=lambda **kwargs: ghost_directive(deps.state, **kwargs),
        ghost_continuity=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
    )


def _ghost_deps(deps: Any, review_deps: ReviewFlowDeps) -> GhostTaskPolicyDeps:
    return GhostTaskPolicyDeps(
        state=deps.state,
        run_ledgers=deps.run_ledgers,
        evidence_ledgers=deps.evidence_ledgers,
        work_checkpoints=deps.work_checkpoints,
        knowledge_store=deps.knowledge_store,
        router_provider_factory=deps.ghost_router_provider_factory,
        learning_provider_factory=deps.ghost_learning_provider_factory,
        learning_modes=tuple(str(item or "").strip() for item in deps.ghost_learning_modes),
        has_reviewable_diff=lambda project: has_reviewable_diff(review_deps, project),
        record_completion_proof_trace=record_completion_proof_trace,
    )


def _project_completion_deps(deps: Any) -> ProjectCompletionDeps:
    return ProjectCompletionDeps(
        state=deps.state,
        agent=AgentAccess(
            run=deps.agent_run,
            capture_provider_failure=deps.capture_provider_failure,
            run_consensus=deps.run_consensus,
            run_project_audit=deps.run_project_audit,
            is_git_repository=deps.is_git_repository or (lambda _project: False),
        ),
        persistence=PersistenceAccess(
            project_facts=deps.project_facts,
            work_checkpoints=deps.work_checkpoints,
            managed_outputs=deps.managed_outputs,
            knowledge_store=deps.knowledge_store,
        ),
        verification=VerificationAccess(
            collect_changes=deps.collect_changes,
            workspace_revisions=deps.workspace_revisions,
        ),
        review=ReviewAccess(
            run=deps.run_review,
            review_fix_turns=deps.review_fix_turns,
            review_log_lines=deps.review_log_lines,
        ),
        runtime=RuntimeAccess(
            mutations=deps.runtime_mutations or getattr(deps.state, "runtime_mutations", None),
            effects=deps.runtime_effects or getattr(deps.state, "runtime_effects", None),
            tool_result_delivery=getattr(deps.state, "tool_result_delivery", None),
        ),
    )


def _dispatch_mode(
    deps: Any,
    project_completion_deps: ProjectCompletionDeps,
    review_deps: ReviewFlowDeps,
    work: RunWork,
    frame: RunFrame,
    hooks: RunHooks,
    task_kind: str,
    config_result: ProjectConfigLoadResult,
) -> ModeOutcome:
    research_deps = _research_deps(deps)

    def run_project_operation(
        active_frame: RunFrame,
        active_work: RunWork,
        active_hooks: RunHooks,
        **kwargs,
    ) -> ModeOutcome:
        return run_project_mode(
            project_completion_deps,
            active_frame,
            active_work,
            active_hooks,
            **kwargs,
        )

    mode_deps = ModeDispatchDeps(
        chat=lambda active_frame: run_chat_mode(
            active_frame,
            state=deps.state,
            run_consensus=deps.run_consensus,
            ghost_directive=lambda **kwargs: ghost_directive(deps.state, **kwargs),
            ghost_continuity=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
        ),
        project=run_project_operation,
        research=lambda active_frame, active_hooks: run_research_mode(
            research_deps,
            active_frame,
            active_hooks,
            proof_question=research_queue_item_title(work.claimed_work_item),
            run_pipeline=lambda pipeline_frame, pipeline_hooks, **kwargs: run_research_pipeline(
                research_deps,
                pipeline_frame,
                pipeline_hooks,
                record_ledger_write=record_evidence_ledger_write,
                **kwargs,
            ),
        ),
        hybrid=lambda active_frame, active_work, active_hooks: run_hybrid_mode(
            research_deps,
            active_frame,
            active_work,
            active_hooks,
            config_result=config_result,
            run_project=run_project_operation,
            run_pipeline=lambda pipeline_frame, pipeline_hooks, **kwargs: run_research_pipeline(
                research_deps,
                pipeline_frame,
                pipeline_hooks,
                record_ledger_write=record_evidence_ledger_write,
                **kwargs,
            ),
        ),
        review=lambda active_frame: run_review_mode(review_deps, active_frame),
        planning=lambda active_frame, active_work, **kwargs: run_planning_readonly_mode(
            _planning_deps(deps),
            active_frame,
            active_work,
            **kwargs,
        ),
    )
    return dispatch_task_mode(
        task_kind,
        frame,
        work,
        hooks,
        mode_deps,
        config_result=config_result,
    )


def _open_ledger(
    deps: Any,
    work: RunWork,
    request: TaskSubmission,
    *,
    run_id: str,
    task_kind: str,
    provider_id: str,
) -> None:
    if (
        request.project
        and task_kind in {"project", "hybrid", "planning_readonly", "review"}
        and deps.run_ledgers is not None
    ):
        try:
            work.ledger = deps.run_ledgers.open(
                run_id=run_id,
                session_id=request.session_id,
                project=request.project,
                task=request.task,
                provider=provider_id,
                mode=ui_mode(task_kind, request.project),
            )
            work.record_agent_events_in_ledger = task_kind in {"project", "planning_readonly"}
        except Exception:
            work.ledger = None


def _start_run_operation(
    deps: Any,
    work: RunWork,
    *,
    session_id: str,
    run_id: str,
    project: str,
    provider_id: str,
    turn_budget: int,
    max_repair_rounds: int,
    task_kind: str,
) -> bool:
    try:
        mutations = deps.runtime_mutations or getattr(deps.state, "runtime_mutations", None)
        if mutations is None:
            raise RuntimeOperationTransitionError("runtime mutation line is missing")
        work.operation = mutations.accept_operation(
            session_id=session_id,
            run_id=run_id,
            project=project,
            provider_id=provider_id,
            turn_budget=turn_budget,
            max_repair_rounds=max_repair_rounds,
            task_kind=task_kind,
        )
        if work.operation is None:
            return False
        return True
    except (OSError, ValueError, RuntimeOperationTransitionError):
        work.operation = None
        return False


def _finish_run_operation(deps: Any, work: RunWork, event: dict[str, object]) -> None:
    if work.operation is None:
        return
    max_turns = int(event.get("max_turns") or 0)
    try:
        mutations = deps.runtime_mutations or getattr(deps.state, "runtime_mutations", None)
        if mutations is None:
            raise RuntimeOperationTransitionError("runtime mutation line is missing")
        work.operation = mutations.mark_terminal(
            work.operation.session_id,
            work.operation.run_id,
            stop_reason=str(event.get("stop_reason") or ""),
            summary_chars=len(str(event.get("summary") or "")),
            turns=terminal_turns(work, turns=event.get("turns"), max_turns=max_turns),
            max_turns=max_turns,
            provider=str(event.get("provider") or ""),
        )
    except (OSError, ValueError, RuntimeOperationTransitionError):
        work.operation = None


def _finish_mode_outcome(
    deps: Any,
    ghost_deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    work: RunWork,
    outcome: ModeOutcome,
    *,
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    finish_trace: Callable[[dict[str, object]], None],
) -> OperationOutcome:
    append_ledger(lambda ledger: ledger.finish(**outcome.event))
    event = event_with_projected_receipt(
        deps.run_ledgers,
        outcome.event,
        session_id=frame.request.session_id,
        run_id=frame.run_id,
    )
    _finish_run_operation(deps, work, event)
    finish_trace(event)
    deps.state.finish_run(frame.run_id, event)
    run_ghost_post_turn(
        ghost_deps,
        frame,
        event,
        work.claimed_work_item,
        research_result=outcome.research_result,
    )
    return operation_outcome_from_task_done_event(event)


def _update_checkpoint_safely(deps: Any, work: RunWork, reason: str) -> None:
    if deps.work_checkpoints is None or work.work_checkpoint is None:
        return
    try:
        work.work_checkpoint = deps.work_checkpoints.set_status(
            work.work_checkpoint,
            "interrupted",
            reason,
        )
    except (OSError, ValueError):
        pass


def _record_route_trace(
    trace_sink: FailOpenPromptTrace,
    *,
    request: TaskSubmission,
    baseline_task_kind: str,
    task_kind: str,
    project: str | None,
    claimed_work_item: GhostWorkItem | None,
    route_result: Any,
    recovered_resume: bool = False,
) -> None:
    route_source = "explicit_user_choice" if str(request.intent or "").strip().lower() != "auto" else "baseline"
    route_reason = "intent_selected" if route_source == "explicit_user_choice" else "baseline_kept"
    route_selected_mode = task_kind
    if recovered_resume:
        route_source = "runtime_recovery"
        route_reason = "safe_tool_replay"
    elif claimed_work_item is not None:
        route_source = "local_work_item"
        route_reason = "claimed_work_item"
    elif route_result is not None:
        route_source = "auto_router"
        route_selected_mode = route_result.selected_mode or route_result.final_mode or task_kind
        route_reason = "accepted" if route_result.accepted else (route_result.skipped_reason or "baseline_kept")
    trace_sink.call(
        "record_router",
        baseline_mode=trace_mode(baseline_task_kind, project),
        selected_mode=trace_mode(route_selected_mode, project),
        final_mode=trace_mode(task_kind, project),
        source=route_source,
        reason_code=route_reason,
        overridden_by_user=(route_source == "explicit_user_choice"),
    )


__all__ = [
    "build_hooks",
    "build_run_work",
    "claim_or_route_ghost_work",
    "connect_and_build_frame",
    "ensure_run_reserved_and_started",
    "finish_mode_outcome",
    "record_provider_failure_event",
    "settle_cancelled_run",
    "settle_error_run",
]
