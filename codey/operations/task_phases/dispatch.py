"""Mode dispatch: deps builders, provider frame, route tracing.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.operations.task_run import TaskRunDeps

from codey.ghost.work_queue_model import GhostWorkItem
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.conversation_plan import plan_conversation
from codey.operations.ghost_context import (
    ghost_continuity,
    ghost_directive,
    ghost_experiences,
)
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
from codey.operations.provider_session import bind_api_usage
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
        allow_switch=not bool(request.model_selection),
    )
    provider = preflight.provider
    provider_id = preflight.provider_id
    bind_api_usage(provider, provider_id, trace)
    bind_display = getattr(provider, "bind_display", None)
    if callable(bind_display):
        def display_event(event: dict[str, Any]) -> None:
            state.emit({**event, "run_id": run_id, "session_id": request.session_id})

        bind_display(display_event)
    context_home = getattr(getattr(state, "managed_outputs", None), "root", None)
    if context_home is not None and callable(getattr(provider, "bind_context", None)):
        provider.bind_context(request.session_id, context_home.parent)
    conversation = state.conversation_for(request.session_id)
    conversation_plan = plan_conversation(
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
    conversation_plan.apply()
    conversation_plan = conversation_plan.activate_model_summary(provider, trace)
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
        window_action=conversation_plan.window_action,
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
        ghost_continuity=partial(ghost_continuity, deps.state),
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
        ghost_directive=partial(ghost_directive, deps.state),
        ghost_continuity=partial(ghost_continuity, deps.state),
        ghost_experiences=partial(ghost_experiences, deps.state),
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
    if request is not None and not frame.project_text and task_kind in {"chat", "auto", "project"}:
        from codey.storage.local_store import DEFAULT_STATE_HOME
        from codey.workspace.session import session_workspace

        home = getattr(deps.workspace_revisions, "state_home", None) or DEFAULT_STATE_HOME
        frame.project_text = str(session_workspace(home, request.session_id))
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
            frame.entry_policy = build_task_policy_for_entry(replace(request, project=frame.project_text or None), task_kind)
        record_entry_policy(getattr(deps, "runtime_mutations", None),
                            session_id=request.session_id, run_id=frame.run_id, policy=frame.entry_policy)
    # Single task entry: project/research/hybrid/readonly share one
    # run_task_mode (same TaskSession/tool loop); ResearchPipeline and project
    # review stay as workflow strategies. Auto's first answer is gated;
    # tool continuations enter the same kernel with the original policy.
    _early = (task_kind or "").strip().lower()
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
            run_pipeline=partial(
                run_research_pipeline, research_deps,
                record_ledger_write=record_evidence_ledger_write,
            ),
        )

    def _run_planning_op(active_frame: RunFrame, active_work: RunWork, **kwargs: Any) -> ModeOutcome:
        return run_planning_readonly_mode(
            _planning_deps(deps),
            active_frame,
            active_work,
            **kwargs,
        )

    if task_kind == "review":
        return run_review_mode(review_deps, frame, append_ledger=hooks.append_ledger)
    return run_task_mode(
        frame, work, hooks, deps, task_kind=task_kind, config_result=config_result,
        run_project=run_project_operation, run_research=_run_research_op, run_planning=_run_planning_op,
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
    route_source = "explicit_user_choice" if (request.intent or "").strip().lower() != "auto" else "baseline"
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
