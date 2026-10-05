"""Writer phase for project completion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, cast

from codey.agents.consensus import render_project_context
from codey.agents.request import AgentRequest
from codey.agents.writer_failover import (
    CheckpointView,
    WriterAttempt,
    WriterFailoverRunner,
)
from codey.operations.ghost_context import ghost_experiences as _ghost_experiences
from codey.operations.project_completion_context import (
    ProjectRun,
    managed_tool_fns,
)
from codey.operations.project_completion_context import (
    commit_runtime_operation as _commit_runtime_operation,
)
from codey.operations.project_completion_context import (
    refresh_checkpoint_view as _refresh_checkpoint_view,
)
from codey.operations.prompting import (
    record_secondary_input_prepared_trace as _record_secondary_input_prepared_trace,
)
from codey.operations.provider_preflight import (
    ensure_provider_fallback_allowed as _ensure_provider_fallback_allowed,
)
from codey.operations.task_context import safe_verification_candidates
from codey.operations.task_execution import build_research_tools as _build_research_tools
from codey.providers.capabilities import rank_providers
from codey.providers.catalog import PROVIDER_LABELS
from codey.providers.diagnostics import ProviderFailure
from codey.providers.supervisor import run_half_open_canary
from codey.reviews.coordinator import change_state
from codey.runtime.core import cancellation
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.task.kind import writer_failover_mode as _writer_failover_mode
from codey.workspace.config import preferred_provider_for


def _writer_verification_candidates(ctx: ProjectRun) -> tuple[Any, ...]:
    return safe_verification_candidates(
        ctx.project,
        ctx.verification_verified_commands,
        ctx.resumed_verification_commands,
        ctx.configured_verification_commands,
        ctx.configured_ignored_paths,
    )


def _on_writer_event(
    ctx: ProjectRun,
    note_turn: Callable[[int], None],
    event: RunEvent,
) -> None:
    note_turn(event.turn)
    ctx.hooks.on_event(event)


def _run_one_writer_attempt(
    ctx: ProjectRun,
    spec: WriterAttempt,
    note_turn: Callable[[int], None],
) -> RunResult:
    workspace_revision_store = ctx.deps.verification.workspace_revisions
    if workspace_revision_store is None:
        raise RuntimeError("coding writer requires WorkspaceRevisionStore")
    ctx.writer_attempt_index += 1
    recovered_outcomes = ctx.frame.recovered_tool_outcomes
    ctx.frame.recovered_tool_outcomes = ()
    settled_outcomes = ctx.frame.settled_tool_outcomes
    ctx.frame.settled_tool_outcomes = ()
    recovered_batch_id = ctx.frame.recovered_tool_result_batch_id
    ctx.frame.recovered_tool_result_batch_id = ""
    try:
        writer_experiences = _ghost_experiences(
            ctx.state,
            session_id=ctx.request.session_id,
            project=ctx.project,
            query=spec.task,
            exclude_run_id=ctx.frame.run_id,
            scope="project",
        )
    except Exception:
        writer_experiences = ""
    return cast(RunResult, ctx.deps.agent.run(AgentRequest(
        provider=spec.provider,
        project=Path(ctx.project),
        task=spec.task,
        max_turns=spec.remaining_turns,
        on_event=partial(_on_writer_event, ctx, note_turn),
        on_shell_request=ctx.hooks.on_shell_request,
        stop_flag=ctx.state.run_registry.stop_flag,
        fresh_chat=spec.fresh_chat,
        change_tracker=ctx.tracker,
        conversation=ctx.frame.conversation,
        provider_id=spec.provider_id,
        handoff=spec.handoff,
        project_facts=ctx.verified_facts,
        research_context=ctx.project_context.research_context,
        project_map=ctx.project_map,
        project_config_warnings=ctx.project_context.project_config_warnings,
        work_checkpoint=spec.checkpoint.prompt,
        verification_candidates=ctx.verification_candidates,
        verification_candidate_loader=partial(_writer_verification_candidates, ctx),
        verification_changed_files=spec.checkpoint.changed_files,
        verification_successful_checks=spec.checkpoint.successful_checks,
        ghost_directive="",
        ghost_continuity="",
        ghost_experiences=str(writer_experiences or ""),
        completion_repair_context=(
            ctx.repair_projection.prompt_text
            if ctx.repair_projection is not None
            else ""
        ),
        completion_repair_context_payload=(
            ctx.repair_projection.to_payload()
            if ctx.repair_projection is not None
            else None
        ),
        permission_profile="coding_writer",
        tool_fns=managed_tool_fns(
            ctx.deps,
            session_id=ctx.request.session_id,
            run_id=ctx.frame.run_id,
        ),
        trace_recorder=ctx.frame.trace,
        session_id=ctx.request.session_id,
        run_id=ctx.frame.run_id,
        effect_scope=f"writer:{ctx.writer_attempt_index}",
        runtime_mutations=ctx.deps.runtime.mutations,
        workspace_revision_store=workspace_revision_store,
        workspace_ignored_paths=ctx.configured_ignored_paths,
        managed_outputs=ctx.deps.persistence.managed_outputs,
        recovered_tool_outcomes=recovered_outcomes,
        settled_tool_outcomes=settled_outcomes,
        recovered_tool_result_batch_id=recovered_batch_id,
        requested_capabilities=ctx.request.requested_capabilities,
        strict_research=bool(getattr(ctx.request, "strict_research", False) is True),
        task_policy=getattr(ctx.frame, "entry_policy", None),
        task_session=ctx.task_session,
        completion_context={
            "execution_evidence": ctx.work.evidence,
            "analysis_run_payloads": ctx.work.analysis_run_payloads,
        },
        project_changes_required=bool(getattr(ctx.request, "project_changes_required", False) is True),
        research_tools=_writer_research_tools(ctx),
    )))


def _writer_research_tools(ctx: ProjectRun) -> Any:
    """Reuse one authorized source adapter across writer repair attempts."""
    from codey.operations.task_entry import build_task_policy_for_entry

    policy = ctx.frame.entry_policy or build_task_policy_for_entry(ctx.request, "project")
    if not any(policy.allows(grant) for grant in ("web.read", "knowledge.read", "knowledge.write", "knowledge.link")):
        return None
    tools = getattr(ctx, "research_tools", None)
    if tools is None:
        tools = _build_research_tools(ctx.deps.persistence, session_id=ctx.request.session_id,
                                      project=ctx.frame.project_text)
        ctx.research_tools = tools
    return tools


def _select_next_writer(ctx: ProjectRun, excluded: set[str]) -> str | None:
    mode = _writer_failover_mode(ctx.frame.task_kind)
    preference = (
        preferred_provider_for(ctx.config_result.config, mode)
        if ctx.config_result is not None
        else ""
    )
    ranked_order = rank_providers(
        ctx.hooks.provider_failover_order(),
        mode=mode,
        preferred=preference,
    )
    if ctx.hooks.supervisor is not None:
        return cast(str | None, ctx.hooks.supervisor.select("", ranked_order, excluded=excluded))
    return next((item for item in ranked_order if item not in excluded), None)


def _capture_writer_failure(
    ctx: ProjectRun,
    pid: str,
    action: str,
    error: BaseException,
) -> ProviderFailure:
    return ctx.deps.agent.capture_provider_failure(
        model=PROVIDER_LABELS.get(pid, pid),
        action=action,
        page=None,
        error=error,
    )


def _on_writer_switch(ctx: ProjectRun, next_provider_id: str) -> None:
    previous_provider_id = ctx.frame.provider_id
    _ensure_provider_fallback_allowed(
        FailOpenPromptTrace(ctx.hooks.trace),
        from_provider=previous_provider_id,
        to_provider=next_provider_id,
        phase="writer_failover",
    )
    ctx.state.switch_run_provider(ctx.frame.run_id, next_provider_id)
    ctx.hooks.append_ledger(
        lambda ledger: ledger.append(
            "provider_switched",
            from_provider=previous_provider_id,
            to_provider=next_provider_id,
            phase="writer_failover",
            reason="provider_failure",
        )
    )
    FailOpenPromptTrace(ctx.hooks.trace).call(
        "record_fallback",
        from_provider=previous_provider_id,
        to_provider=next_provider_id,
        phase="writer_failover",
        reason_code="provider_failure",
    )
    ctx.frame.conversation.update_snapshot(
        replace(
            ctx.frame.conversation.snapshot,
            provider_id=next_provider_id,
            blocker="",
        )
    )


def _close_provider(item: Any) -> None:
    item.close()


def _needs_no_canary(_pid: str) -> bool:
    return False


def _record_no_success(_pid: str) -> None:
    return None


def _clear_provider_session(ctx: ProjectRun, pid: str) -> None:
    ctx.state.set_provider_session(pid, None)


def _run_writer_canary(ctx: ProjectRun, pid: str, item: Any) -> bool:
    assert ctx.hooks.supervisor is not None
    return run_half_open_canary(pid, item, ctx.hooks.supervisor)


def _build_writer_failover(ctx: ProjectRun) -> WriterFailoverRunner:
    assert ctx.tried_writers is not None
    # Function-level: task_phases.dispatch imports this module, so a
    # top-level import would cycle.
    from codey.operations.task_phases.hooks import (
        record_provider_success_event as _record_provider_success_event,
    )

    return WriterFailoverRunner(
        provider=ctx.frame.provider,
        provider_id=ctx.frame.provider_id,
        switches=ctx.frame.preflight_switches,
        tried=ctx.tried_writers,
        attempt=partial(_run_one_writer_attempt, ctx),
        select_next=partial(_select_next_writer, ctx),
        connect=ctx.state.get_provider,
        close=_close_provider,
        needs_canary=(
            ctx.hooks.supervisor.needs_canary
            if ctx.hooks.supervisor is not None
            else _needs_no_canary
        ),
        run_canary=partial(_run_writer_canary, ctx),
        capture_failure=partial(_capture_writer_failure, ctx),
        record_failure=ctx.hooks.record_provider_failure,
        record_success=(
            partial(_record_provider_success_event, ctx.hooks.supervisor)
            if ctx.hooks.supervisor is not None
            else _record_no_success
        ),
        clear_session=partial(_clear_provider_session, ctx),
        on_switch=partial(_on_writer_switch, ctx),
        refresh_checkpoint=partial(_refresh_checkpoint_view, ctx),
        stopped=ctx.state.run_registry.stop_flag.is_set,
    )


def _sync_failover_frame(ctx: ProjectRun) -> None:
    assert ctx.failover is not None
    ctx.frame.provider = ctx.failover.provider
    ctx.frame.provider_id = ctx.failover.provider_id
    ctx.frame.preflight_switches = ctx.failover.switches


def _maybe_consult_consensus_after_writer(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    if (
        ctx.deps.agent.run_consensus is None
        or ctx.used_project_audit
        or ctx.result.stop_reason != "done"
        or ctx.result.changed
        or ctx.state.run_registry.stop_flag.is_set()
    ):
        return
    context = render_project_context(
        ctx.frame.conversation.snapshot,
        ctx.verified_facts,
        draft=ctx.result.summary,
        project_map=ctx.project_map,
    )
    try:
        _record_secondary_input_prepared_trace(
            ctx.frame.trace,
            "consensus",
            task=ctx.request.task,
            context=context,
            draft=ctx.result.summary,
        )
        consulted = ctx.deps.agent.run_consensus(
            selected_provider=ctx.frame.provider,
            selected_provider_id=ctx.frame.provider_id,
            task=ctx.request.task,
            context=context,
            draft=ctx.result.summary,
            trace_recorder=ctx.frame.trace,
        )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        ctx.state.set_provider_session(ctx.frame.provider_id, None)
        consulted = None
    if consulted is not None:
        if consulted.degraded:
            ctx.state.set_provider_session(ctx.frame.provider_id, None)
        ctx.result = replace(ctx.result, summary=consulted.answer)


def _run_writer_phase(ctx: ProjectRun) -> None:
    ctx.failover = _build_writer_failover(ctx)
    assert ctx.failover is not None
    _commit_runtime_operation(
        ctx,
        "mark_writer_running",
        lambda mutations, session_id, run_id: mutations.mark_writer_running(
            session_id,
            run_id,
            provider_id=ctx.frame.provider_id,
        ),
    )
    try:
        ctx.result = ctx.failover.run(
            task=ctx.agent_task,
            turn_budget=ctx.request.max_turns,
            fresh=ctx.agent_fresh_chat,
            handoff=ctx.frame.handoff,
            checkpoint=CheckpointView(
                prompt=ctx.checkpoint_prompt,
                changed_files=ctx.resumed_changed_files,
                successful_checks=ctx.resumed_successful_checks,
            ),
        )
    finally:
        _sync_failover_frame(ctx)
    assert ctx.result is not None
    result = ctx.result
    # Approval and cancellation can retain an unexecuted or uncertain intent.
    # The outer lifecycle terminalizes them without claiming tool settlement.
    if result.stop_reason not in {"approval", "stopped"}:
        _commit_runtime_operation(
            ctx,
            "mark_writer_settled",
            lambda mutations, session_id, run_id: mutations.mark_writer_settled(
                session_id,
                run_id,
                provider_id=ctx.frame.provider_id,
                turns_used=result.turns,
                stop_reason=result.stop_reason,
            ),
        )
    ctx.inherited_green = bool(
        ctx.project_context.checkpoint.resumed
        and ctx.work.work_checkpoint is not None
        and not ctx.result.changed
        and not ctx.result.checks_ran
        and ctx.work.evidence.has_successful_checks
    )
    # 共同 gate 证明随 writer 结果保留：外层只持久化与展示，不再另造。
    ctx.task_session = ctx.result.facts
    if ctx.inherited_green:
        ctx.result = replace(ctx.result, checks_passed=True)
    checkpoint_changed = bool(
        ctx.work.work_checkpoint is not None
        and ctx.work.work_checkpoint.changed_files
    )
    ctx.task_changed = ctx.result.changed or checkpoint_changed
    ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    collected_changed = change_state(ctx.task_changes)
    ctx.task_changes_dirty = collected_changed is None
    if collected_changed is not None:
        ctx.task_changed = collected_changed
    if ctx.result.stop_reason == "done":
        ctx.hooks.update_checkpoint(
            lambda store, item: store.set_status(item, "ready_for_review")
        )
    ctx.state.set_provider_session(
        ctx.frame.provider_id,
        None if ctx.result.stop_reason == "stopped" else ctx.request.session_id,
    )
    _maybe_consult_consensus_after_writer(ctx)


def run_writer_phase(ctx: ProjectRun) -> None:
    """Public writer phase entry."""
    _run_writer_phase(ctx)


__all__ = [
    "run_writer_phase",
]
