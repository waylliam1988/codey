"""Mode dispatch: deps builders, provider frame, route tracing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.operations.task_run import TaskRunDeps

from codey.ghost.work_queue_model import GhostWorkItem
from codey.operations.auto_loop import (
    AutoRunDeps,
    is_auto_request,
    run_auto_mode,
)
from codey.operations.chat import run_chat_mode
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.ghost_context import (
    ghost_continuity,
    ghost_directive,
    ghost_experiences,
)
from codey.operations.mode_dispatch import ModeDispatchDeps, dispatch_task_mode
from codey.operations.planning_flow import (
    PlanningFlowDeps,
    run_planning_readonly_mode,
)
from codey.operations.project_completion_context import (
    AgentAccess,
    PersistenceAccess,
    ProjectCompletionDeps,
    ReviewAccess,
    RuntimeAccess,
    VerificationAccess,
)
from codey.operations.project_completion_flow import run_project_mode
from codey.operations.provider_preflight import connect_provider_with_preflight
from codey.operations.recovery import rebuilt_policy_from_log, record_entry_policy
from codey.operations.research_flow import (
    ResearchFlowDeps,
    record_evidence_ledger_write,
    research_queue_item_title,
    run_research_mode,
    run_research_pipeline,
)
from codey.operations.result import ModeOutcome
from codey.operations.review_flow import ReviewFlowDeps, run_review_mode
from codey.operations.task_entry import build_task_policy_for_entry, run_entry_kernel, run_task_mode
from codey.operations.task_state import TaskState
from codey.providers.capabilities import rank_providers
from codey.research.search_factory import default_research_search_provider
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.task.kind import startup_failover_mode, trace_mode
from codey.task.model import TaskSubmission
from codey.workspace.config import ProjectConfigLoadResult, preferred_provider_for


def connect_and_build_frame(
    deps: TaskRunDeps,
    state: TaskState,
    request: TaskSubmission,
    *,
    run_id: str,
    task_kind: str,
    project: str | None,
    provider_id: str,
    trace: Any,
    trace_sink: FailOpenPromptTrace,
    hooks: RunHooks,
    project_config_result: ProjectConfigLoadResult,
    recovered_tool_outcomes: tuple[Any, ...] = (),
    recovered_tool_result_batch_id: str = "",
    settled_tool_outcomes: tuple[Any, ...] = (),
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
        connect=deps.connect_provider,
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
        settled_tool_outcomes=settled_tool_outcomes,
    )
    return frame, provider, provider_id


def review_flow_deps(deps: TaskRunDeps) -> ReviewFlowDeps:
    return ReviewFlowDeps(
        state=deps.state,
        collect_changes=deps.collect_changes,
        run_review=deps.run_review,
        is_git_repository=deps.is_git_repository or (lambda _project: False),
        review_log_lines=deps.review_log_lines,
    )


def _research_deps(deps: TaskRunDeps) -> ResearchFlowDeps:
    return ResearchFlowDeps(
        state=deps.state,
        knowledge_store=deps.knowledge_store,
        evidence_ledgers=deps.evidence_ledgers,
        search_factory=deps.search_factory or default_research_search_provider,
        run_research_advisors=deps.run_research_advisors,
        ghost_continuity=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
        managed_outputs=deps.managed_outputs,
        runtime_mutations=deps.runtime_mutations,
    )


def _planning_deps(deps: TaskRunDeps) -> PlanningFlowDeps:
    return PlanningFlowDeps(
        state=deps.state,
        agent_run=deps.agent_run,
        project_facts=deps.project_facts,
        knowledge_store=deps.knowledge_store,
        search_factory=deps.search_factory,
        review_log_lines=deps.review_log_lines,
        ghost_directive=lambda **kwargs: ghost_directive(deps.state, **kwargs),
        ghost_continuity=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
        ghost_experiences=lambda **kwargs: ghost_experiences(deps.state, **kwargs),
    )


def project_completion_deps(deps: TaskRunDeps) -> ProjectCompletionDeps:
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
            search_factory=deps.search_factory,
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
            mutations=deps.runtime_mutations,
            effects=deps.runtime_effects,
            tool_result_delivery=getattr(deps.state, "tool_result_delivery", None),
        ),
    )


def dispatch_run_mode(
    deps: TaskRunDeps,
    project_completion_deps: ProjectCompletionDeps,
    review_deps: ReviewFlowDeps,
    work: RunWork,
    frame: RunFrame,
    hooks: RunHooks,
    task_kind: str,
    config_result: ProjectConfigLoadResult,
) -> ModeOutcome:
    request = getattr(frame, "request", None)
    if request is not None:
        previous = str(getattr(request, "previous_run_id", "") or "")
        operation = getattr(work, "operation", None)
        recovering = bool(frame.recovered_tool_outcomes or frame.settled_tool_outcomes
                          or getattr(operation, "task_policy", {})
                          or (operation is not None and operation.leaf != "accepted"))
        if previous or recovering:
            mutations = getattr(deps, "runtime_mutations", None)
            frame.entry_policy = rebuilt_policy_from_log(
                getattr(mutations, "session_log", None),
                session_id=request.session_id, run_id=previous or frame.run_id,
            )
        else:
            frame.entry_policy = build_task_policy_for_entry(request, task_kind)
        record_entry_policy(getattr(deps, "runtime_mutations", None),
                            session_id=request.session_id, run_id=frame.run_id, policy=frame.entry_policy)
    # Single task entry: project/research/hybrid/readonly share one
    # run_task_mode (same TaskSession/tool loop); ResearchPipeline and project
    # review stay as workflow strategies. Auto's first answer is gated;
    # tool continuations enter the same kernel with the original policy.
    _early = str(task_kind or "").strip().lower()
    if _early != "project" and (frame.recovered_tool_outcomes or frame.settled_tool_outcomes):
        return run_entry_kernel(frame, work, hooks, deps, task_kind=task_kind, config_result=config_result)
    if _early == "hybrid":
        return run_task_mode(
            frame, work, hooks, deps,
            task_kind="hybrid",
            config_result=config_result,
        )

    research_deps = _research_deps(deps)

    def run_project_operation(
        active_frame: RunFrame,
        active_work: RunWork,
        active_hooks: RunHooks,
        **kwargs: Any,
    ) -> ModeOutcome:
        return run_project_mode(
            project_completion_deps,
            active_frame,
            active_work,
            active_hooks,
            **kwargs,
        )

    def _run_research_op(active_frame: RunFrame, active_hooks: RunHooks) -> ModeOutcome:
        return run_research_mode(
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
        )

    def _run_planning_op(active_frame: RunFrame, active_work: RunWork, **kwargs: Any) -> ModeOutcome:
        return run_planning_readonly_mode(
            _planning_deps(deps),
            active_frame,
            active_work,
            **kwargs,
        )

    mode_deps = ModeDispatchDeps(
        chat=lambda active_frame: run_chat_mode(
            active_frame,
            state=deps.state,
            run_consensus=deps.run_consensus,
            ghost_directive=lambda **kwargs: ghost_directive(deps.state, **kwargs),
            ghost_continuity=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
            ghost_experiences=lambda **kwargs: ghost_experiences(deps.state, **kwargs),
        ),        project=run_project_operation,
        research=_run_research_op,
        review=lambda active_frame: run_review_mode(
            review_deps,
            active_frame,
            append_ledger=hooks.append_ledger,
        ),
        planning=_run_planning_op,
    )
    # Unified auto runs in the same batch as the Ghost-route removal: the first
    # normal model call decides direct answer vs permission-checked action, so
    # unprojected auto research never degrades to pure chat. Recovered tool
    # replays and claimed Ghost work items bypass auto: their mode is already
    # decided and the first output must not override it.
    if (
        is_auto_request(frame.request)
        and not frame.recovered_tool_outcomes
        and not frame.settled_tool_outcomes
        and work.claimed_work_item is None
    ):
        from codey.operations.review_flow import has_reviewable_diff as _has_diff
        from codey.operations.task_phases.lifecycle import open_run_ledger as _open_ledger

        auto_deps = AutoRunDeps(
            state=deps.state,
            review_task=mode_deps.review,
            acquire_writer=lambda project: deps.state.acquire_project_writer(project),
            release_writer=lambda project: deps.state.release_project_writer(project),
            open_ledger_for=lambda kind: _open_ledger(
                deps, work, frame.request,
                run_id=frame.run_id, task_kind=kind, provider_id=frame.provider_id,
            ),
            ghost_directive_fn=lambda **kwargs: ghost_directive(deps.state, **kwargs),
            ghost_continuity_fn=lambda **kwargs: ghost_continuity(deps.state, **kwargs),
            experiences_fn=lambda **kwargs: ghost_experiences(deps.state, **kwargs),
            has_reviewable_diff_fn=lambda: _has_diff(review_deps, frame.request.project),
            research_available=True,
            continue_task=lambda active_frame, active_work, active_hooks, followup: run_entry_kernel(
                active_frame, active_work, active_hooks, deps,
                task_kind=active_frame.entry_session.task_kind,
                config_result=config_result, continuation_followup=followup,
            ),
        )
        return run_auto_mode(frame, work, hooks, auto_deps)
    # Single entry for task kinds (no old two-phase hybrid). This stays
    # after the auto check: auto intent must reach the first-call router
    # before any mode entry, otherwise the writer runs directly.
    _normalized = str(task_kind or "").strip().lower()
    if _normalized in {"project", "research", "hybrid", "planning", "planning_readonly", "readonly"}:
        return run_task_mode(
            frame, work, hooks, deps,
            task_kind=task_kind,
            config_result=config_result,
            run_project=run_project_operation,
            run_research=_run_research_op,
            run_planning=_run_planning_op,
        )
    return dispatch_task_mode(
        task_kind,
        frame,
        work,
        hooks,
        mode_deps,
        config_result=config_result,
    )


def record_route_trace(
    trace_sink: FailOpenPromptTrace,
    *,
    request: TaskSubmission,
    baseline_task_kind: str,
    task_kind: str,
    project: str | None,
    claimed_work_item: GhostWorkItem | None,
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
    trace_sink.call(
        "record_mode_selection",
        baseline_mode=trace_mode(baseline_task_kind, project),
        selected_mode=trace_mode(route_selected_mode, project),
        final_mode=trace_mode(task_kind, project),
        source=route_source,
        reason_code=route_reason,
        overridden_by_user=(route_source == "explicit_user_choice"),
    )
