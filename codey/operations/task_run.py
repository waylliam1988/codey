"""Task run lifecycle.

Thin orchestrator around task_phases: run slot, provider task context,
trace/ledger wiring, and terminal settlement. Phase helpers live in
task_phases (split by concern: lifecycle, ghost, hooks, dispatch,
settlement) so this module stays under the long-file guardrail.
Mode behavior lives in the mode flow modules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from typing import Any

from codey.ghost.work_queue import GhostWorkItem
from codey.operations.context import RunFrame, RunWork
from codey.operations.ghost_post_turn import release_work_item, run_ghost_post_turn
from codey.operations.recovery import recover_effects_for_resume
from codey.operations.task_phases import (
    dispatch_run_mode,
    finish_run_operation,
    ghost_task_deps,
    open_run_ledger,
    open_run_trace,
    project_completion_deps,
    record_route_trace,
    review_flow_deps,
    start_run_operation,
    build_hooks,
    build_run_work,
    claim_or_route_ghost_work,
    connect_and_build_frame,
    ensure_run_reserved_and_started,
    finish_mode_outcome,
    record_provider_failure_event,
    settle_cancelled_run,
    settle_error_run,
)
from codey.providers import controls as provider_controls
from codey.providers import flow as provider_flow
from codey.runtime.core import cancellation
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.runtime.observe.terminalizer import (
    operation_outcome_from_task_done_event,
    task_done_event,
)
from codey.task.kind import resolve_task_kind, trace_mode, ui_mode
from codey.task.model import TaskSubmission
from codey.workspace.config import ProjectConfigLoadResult, load_project_config


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskRunDeps:
    state: Any
    agent_run: Callable
    collect_changes: Callable
    run_review: Callable
    capture_provider_failure: Callable
    workspace_revisions: Any
    run_consensus: Callable | None = None
    run_project_audit: Callable | None = None
    run_research_advisors: Callable | None = None
    project_facts: Any = None
    work_checkpoints: Any = None
    run_ledgers: Any = None
    run_traces: Any = None
    evidence_ledgers: Any = None
    managed_outputs: Any = None
    knowledge_store: Any = None
    search_factory: Callable[[], object] | None = None
    is_git_repository: Callable[[str | Path], bool] | None = None
    review_fix_turns: int = 12
    review_log_lines: int = 80
    ghost_learning_provider_factory: Callable[[str], Any] | None = None
    ghost_learning_modes: tuple[str, ...] = ("chat",)
    ghost_router_provider_factory: Callable[[str], Any] | None = None
    runtime_mutations: Any = None
    runtime_effects: Any = None


def prepare_submission(state: Any, request: TaskSubmission) -> TaskSubmission | None:
    if request.run_id:
        active = state.current_run()
        if active is None:
            reserved = state.reserve_run(
                session_id=request.session_id,
                project=request.project,
                task=request.task,
                provider_id=request.provider_id,
                run_id=request.run_id,
            )
            return request if reserved is not None else None
        # A preset run_id must match the active run; otherwise the slot is
        # busy with somebody else. Previously any preset id bypassed the busy
        # check and produced orphan operations downstream.
        if active.run_id != request.run_id:
            return None
        return request
    reserved = state.reserve_run(
        session_id=request.session_id,
        project=request.project,
        task=request.task,
        provider_id=request.provider_id,
    )
    if reserved is None:
        return None
    return replace(request, run_id=reserved.run_id)


def release_unstarted_submission(state: Any, request: TaskSubmission) -> None:
    if not request.run_id:
        return
    try:
        state.release_run(request.run_id)
    except Exception:
        return


def execute_task_run(deps: TaskRunDeps, request: TaskSubmission) -> OperationOutcome | None:
    state = deps.state
    session_id = request.session_id
    project = request.project
    task = request.task
    max_turns = request.max_turns
    continue_task = request.continue_task
    provider_id = request.provider_id
    baseline_task_kind = resolve_task_kind(request)
    task_kind = baseline_task_kind
    run_id = request.run_id
    claimed_work_item: GhostWorkItem | None = None
    frame: RunFrame | None = None
    provider: Any | None = None
    conversation = None
    work: RunWork | None = None

    reservation, aborted = ensure_run_reserved_and_started(state, request)
    if reservation is None:
        return aborted
    request = reservation.request
    run_id = reservation.run_id

    trace = open_run_trace(deps, session_id, run_id, project, baseline_task_kind, provider_id)
    trace_sink = FailOpenPromptTrace(trace)
    project_config_result = load_project_config(project) if project else ProjectConfigLoadResult()

    def current_provider_id() -> str:
        return frame.provider_id if frame is not None else provider_id

    def current_provider() -> Any | None:
        return frame.provider if frame is not None else provider

    def finish_trace(event: dict[str, object]) -> None:
        trace_sink.call(
            "finish",
            status=str(event.get("stop_reason") or "done"),
            mode=trace_mode(task_kind, project),
            provider=str(event.get("provider") or current_provider_id()),
        )

    provider_controls.set_teach_handler(state.handle_control_teach)
    provider_controls.set_doctor_handler(getattr(state, "handle_profile_doctor", None))
    provider_flow.set_recovery_handler(getattr(state, "handle_flow_recovery", None))
    provider_controls.begin_task_context(session_id)
    state.run_registry.set_last_provider_failure(None)
    previous_cancel_event = cancellation.set_event(state.run_registry.stop_flag)

    review_deps = review_flow_deps(deps)
    ghost_deps = ghost_task_deps(deps, review_deps)
    route_result = None

    def _fail_early(
        summary_text: str,
        *,
        current_work: RunWork | None = None,
    ) -> OperationOutcome:
        error_event = task_done_event(
            run_id=run_id,
            session_id=session_id,
            summary=summary_text,
            stop_reason="error",
            max_turns=max_turns,
            provider=provider_id,
            mode=ui_mode(task_kind, project),
            work=current_work,
        )
        if current_work is not None:
            finish_run_operation(deps, current_work, error_event)
        finish_trace(error_event)
        state.finish_run(run_id, error_event)
        run_ghost_post_turn(
            ghost_deps,
            None,
            error_event,
            current_work.claimed_work_item if current_work is not None else claimed_work_item,
            project_text=str(project or ""),
        )
        return operation_outcome_from_task_done_event(error_event)

    recovered_tool_outcomes = ()
    recovered_tool_result_batch_id = ""
    try:
        work, _workspace_state = build_run_work(
            deps,
            session_id=session_id,
            run_id=run_id,
            project=project,
            provider_id=provider_id,
            max_turns=max_turns,
            task_kind=task_kind,
            ignored_paths=project_config_result.config.ignored_paths,
            trace=trace,
        )
        if work is None:
            empty_work = RunWork(
                recent_events=[],
                evidence=ExecutionEvidence(
                    workspace_revision=_workspace_state.revision,
                    workspace_fingerprint=_workspace_state.fingerprint,
                ),
                claimed_work_item=claimed_work_item,
                trace=trace,
                workspace_revision=_workspace_state.revision,
                workspace_fingerprint=_workspace_state.fingerprint,
            )
            return _fail_early("ERROR: Runtime operation state unavailable.", current_work=empty_work)

        recovery = recover_effects_for_resume(
            deps,
            session_id=session_id,
            run_id=run_id,
            project=project or "",
            task_kind=baseline_task_kind,
        )
        if not recovery.ok:
            return _fail_early(
                "ERROR: Runtime recovery failed to settle unconfirmed effects.",
                current_work=work,
            )
        recovered_tool_outcomes = recovery.recovered_tool_outcomes
        recovered_tool_result_batch_id = recovery.recovered_tool_result_batch_id
        recovered_resume = bool(recovered_tool_outcomes)
        if recovered_resume:
            task_kind = "project"
            if not continue_task:
                request = replace(request, continue_task=True)
                continue_task = True

        try:
            if not recovered_tool_outcomes:
                claimed = claim_or_route_ghost_work(
                    ghost_deps, request,
                    baseline_task_kind=baseline_task_kind, run_id=run_id,
                )
                request = claimed.request
                task_kind = claimed.task_kind
                claimed_work_item = claimed.claimed_work_item
                route_result = claimed.route_result
                if work is not None:
                    work.claimed_work_item = claimed_work_item
                task = request.task
                continue_task = request.continue_task
        except cancellation.TaskCancelled:
            state.set_provider_session(provider_id, None)
            release_work_item(
                ghost_deps,
                claimed_work_item,
                run_id=run_id,
                reason="stopped_before_start",
            )
            stopped_event = task_done_event(
                run_id=run_id,
                session_id=session_id,
                summary="",
                stop_reason="stopped",
                max_turns=max_turns,
                provider=provider_id,
                mode=ui_mode(baseline_task_kind, project),
            )
            trace_sink.call(
                "record_router",
                baseline_mode=trace_mode(baseline_task_kind, project),
                selected_mode=trace_mode(task_kind, project),
                final_mode=trace_mode(task_kind, project),
                source="local_work_item" if claimed_work_item else "baseline",
                reason_code="stopped_before_start",
            )
            finish_trace(stopped_event)
            state.finish_run(run_id, stopped_event)
            return operation_outcome_from_task_done_event(stopped_event)

        record_route_trace(
            trace_sink,
            request=request,
            baseline_task_kind=baseline_task_kind,
            task_kind=task_kind,
            project=project,
            claimed_work_item=claimed_work_item,
            route_result=route_result,
            recovered_resume=recovered_resume,
        )
        state.emit({
            "type": "task_start",
            "run_id": run_id,
            "session_id": session_id,
            "project": project,
            "task": task,
            "mode": ui_mode(task_kind, project),
            "max_turns": max_turns,
            "continue_task": continue_task,
            "provider": provider_id,
            "intent": request.intent,
        })

        completion_deps = project_completion_deps(deps)
        open_run_ledger(deps, work, request, run_id=run_id, task_kind=task_kind, provider_id=provider_id)

        hooks = build_hooks(
            deps,
            state,
            work,
            session_id=session_id,
            run_id=run_id,
            project=project,
            max_turns=max_turns,
            project_config_ignored=project_config_result.config.ignored_paths,
            review_log_lines=deps.review_log_lines,
            project_completion_deps=completion_deps,
            current_provider_id=lambda: frame.provider_id if frame is not None else provider_id,
        )

        if task_kind == "review":
            project_text = str(Path(project).expanduser().resolve()) if project else ""
            conversation = state.conversation_for(session_id)
            frame = RunFrame(
                request=request,
                run_id=run_id,
                task_kind=task_kind,
                provider=None,
                provider_id=provider_id,
                project_text=project_text,
                conversation=conversation,
                fresh_chat=False,
                handoff="",
                research_handoff="",
                prior_snapshot=conversation.snapshot,
                recovered_owner_prompt="",
                provider_session_changed=False,
                preflight_tried=set(),
                preflight_switches=0,
                trace=trace,
            )
            outcome = dispatch_run_mode(
                deps,
                completion_deps,
                review_deps,
                work,
                frame,
                hooks,
                task_kind,
                project_config_result,
            )
            return finish_mode_outcome(
                deps,
                ghost_deps,
                frame,
                work,
                outcome,
                append_ledger=hooks.append_ledger,
                finish_trace=finish_trace,
            )

        frame, provider, provider_id = connect_and_build_frame(
            deps,
            state,
            request,
            work,
            run_id=run_id,
            task_kind=task_kind,
            project=project,
            provider_id=provider_id,
            trace=trace,
            trace_sink=trace_sink,
            hooks=hooks,
            project_config_result=project_config_result,
            recovered_tool_outcomes=recovered_tool_outcomes,
            recovered_tool_result_batch_id=recovered_tool_result_batch_id,
        )
        conversation = frame.conversation

        outcome = dispatch_run_mode(
            deps,
            completion_deps,
            review_deps,
            work,
            frame,
            hooks,
            task_kind,
            project_config_result,
        )
        return finish_mode_outcome(
            deps,
            ghost_deps,
            frame,
            work,
            outcome,
            append_ledger=hooks.append_ledger,
            finish_trace=finish_trace,
        )
    except cancellation.TaskCancelled:
        return settle_cancelled_run(
            deps,
            state,
            work,
            ghost_deps,
            run_id=run_id,
            session_id=session_id,
            max_turns=max_turns,
            provider_id=current_provider_id(),
            task_kind=task_kind,
            project=project,
            frame=frame,
            finish_trace=finish_trace,
            conversation=conversation,
        )
    except Exception as exc:
        return settle_error_run(
            deps,
            state,
            exc,
            work,
            ghost_deps,
            run_id=run_id,
            session_id=session_id,
            max_turns=max_turns,
            provider_id=current_provider_id(),
            task_kind=task_kind,
            project=project,
            frame=frame,
            provider=current_provider(),
            finish_trace=finish_trace,
            conversation=conversation,
        )
    finally:
        cancellation.set_event(previous_cancel_event)
        provider_controls.end_task_context()
        try:
            current_item = current_provider()
            if current_item is not None:
                current_item.close()
        except Exception:
            pass




__all__ = [
    "TaskRunDeps",
    "start_run_operation",
    "execute_task_run",
    "prepare_submission",
    "record_provider_failure_event",
    "release_unstarted_submission",
]
