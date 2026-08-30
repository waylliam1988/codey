"""Planning-readonly task operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codey.agents.request import AgentRequest
from codey.operations.context import RunFrame, RunWork
from codey.operations.prompting import record_local_context_trace
from codey.operations.result import ModeOutcome
from codey.runtime.events import RunEvent, render_run_event, run_event_ui_payload
from codey.workspace.config import ProjectConfigLoadResult
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.task_context import ProjectTaskContextBuilder


@dataclass(frozen=True)
class PlanningFlowDeps:
    state: Any
    agent_run: Callable
    project_facts: ProjectFactsStore | None = None
    knowledge_store: Any = None
    review_log_lines: int = 80
    ghost_directive: Callable[..., Any] | None = None
    ghost_continuity: Callable[..., Any] | None = None


def run_planning_readonly_mode(
    deps: PlanningFlowDeps,
    frame: RunFrame,
    work: RunWork,
    *,
    config_result: ProjectConfigLoadResult | None = None,
) -> ModeOutcome:
    state = deps.state
    request = frame.request
    project = request.project
    if project is None:
        raise RuntimeError("planning_readonly mode requires a project")
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    if work.ledger is not None:
        work.record_agent_events_in_ledger = True
    context_builder = ProjectTaskContextBuilder(
        project_facts=deps.project_facts,
        work_checkpoints=None,
        knowledge_store=deps.knowledge_store,
        config_result=config_result,
    )
    project_context = context_builder.build(
        project=project,
        task=request.task,
        session_id=request.session_id,
        run_id=frame.run_id,
        continue_task=False,
        provider_session_changed=frame.provider_session_changed,
    )
    ghost_directive = deps.ghost_directive(
        project=project,
        session_id=request.session_id,
    )
    ghost_continuity = deps.ghost_continuity(
        project=project,
        session_id=request.session_id,
    )
    record_local_context_trace(frame.trace, ghost_directive, ghost_continuity)
    result = deps.agent_run(AgentRequest(
        provider=frame.provider,
        project=Path(project),
        task=request.task,
        max_turns=request.max_turns,
        on_event=lambda event: record_planning_event(deps, frame, work, event),
        on_shell_request=None,
        stop_flag=state.run_registry.stop_flag,
        fresh_chat=frame.fresh_chat,
        strict_fresh_chat=False,
        change_tracker=None,
        conversation=frame.conversation,
        provider_id=frame.provider_id,
        handoff=frame.handoff,
        project_facts=project_context.verified_facts,
        research_context=project_context.research_context,
        project_map=project_context.project_map,
        project_config_warnings=project_context.project_config_warnings,
        ghost_directive=ghost_directive.text,
        ghost_continuity=ghost_continuity.text,
        permission_profile="planning_readonly",
        trace_recorder=frame.trace,
    ))
    state.set_provider_session(
        frame.provider_id,
        None if result.stop_reason == "stopped" else request.session_id,
    )
    frame.conversation.update_snapshot(
        replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            checks_passed=False,
            summary=result.summary,
            blocker="" if result.stop_reason == "done" else result.summary,
        )
    )
    return ModeOutcome(
        {
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "planning",
            "changed": False,
        }
    )


def record_planning_event(
    deps: PlanningFlowDeps,
    frame: RunFrame,
    work: RunWork,
    event: RunEvent,
) -> None:
    if work.record_agent_events_in_ledger and work.ledger is not None:
        try:
            work.ledger.append_run_event(event)
        except Exception:
            work.ledger = None
    payload = run_event_ui_payload(frame.run_id, frame.request.session_id, event)
    if payload is not None:
        deps.state.emit(payload)
    if event.kind == "tool_start":
        return
    work.evidence.record(event)
    message = render_run_event(event)
    work.recent_events.append(message)
    if len(work.recent_events) > deps.review_log_lines * 2:
        del work.recent_events[: deps.review_log_lines]


__all__ = [
    "PlanningFlowDeps",
    "record_planning_event",
    "run_planning_readonly_mode",
]
