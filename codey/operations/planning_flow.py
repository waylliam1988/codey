"""Planning-readonly task operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.research.tools import ResearchTools

from codey.agents.request import AgentRequest
from codey.knowledge.store import KnowledgeStore
from codey.operations.context import RunFrame, RunWork
from codey.operations.result import ModeOutcome
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.operations.task_state import TaskState, stop_flag_for
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import ChatProvider
from codey.providers.transport import ProviderTransport
from codey.runs.trace import RunTraceRecorder
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.events import RunEvent, render_run_event, run_event_ui_payload
from codey.storage.managed_outputs import ManagedOutputStore
from codey.task.model import TaskSubmission
from codey.workspace.config import ProjectConfigLoadResult
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.revision import WorkspaceRevisionStore


@dataclass(frozen=True)
class PlanningFlowDeps:
    state: TaskState
    agent_run: Callable[[AgentRequest], RunResult]
    project_facts: ProjectFactsStore | None = None
    knowledge_store: KnowledgeStore | None = None
    search_factory: Callable[[], object] | None = None
    review_log_lines: int = 80


def _planning_entry_policy(frame: RunFrame, request: TaskSubmission) -> TaskPolicy:
    """Require a read-only policy before constructing execution resources."""
    from codey.operations.entry_policy import build_task_policy_for_entry

    policy = frame.entry_policy
    if policy is None:
        policy = build_task_policy_for_entry(request, "planning")
    readonly_grants = policy.grants & {"control", "project.read", "web.read", "knowledge.read", "browser.observe"}
    if readonly_grants != policy.grants:
        policy = replace(policy, grants=frozenset(readonly_grants), source=policy.source + ";planning_readonly")
    return policy


def _planning_research_tools(deps: PlanningFlowDeps, policy: TaskPolicy, *, session_id: str, project: str) -> ResearchTools | None:
    """Construct source resources only when the resolved policy permits them."""
    from codey.operations.task_execution import build_research_tools

    if not policy.allows("web.read") and not policy.allows("knowledge.read"):
        return None
    return build_research_tools(deps, session_id=session_id, project=project,
                                browser_actions=policy.allows("browser.observe"))


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
    provider: ChatProvider | ProviderTransport | None = frame.provider
    trace: RunTraceRecorder | None = frame.trace
    if provider is None:
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
    entry_policy = _planning_entry_policy(frame, request)
    # 共同任务准备：策略一次确定、run 身份、授权资源、恢复行全部传入，
    # planning 只收窄写权限并提供规划上下文。
    from codey.operations.task_execution import close_research_tools

    research_tools = _planning_research_tools(
        deps, entry_policy, session_id=request.session_id, project=project,
    )
    workspace_revisions: WorkspaceRevisionStore | None = getattr(state, "workspace_revisions", None)
    managed_outputs: ManagedOutputStore | None = getattr(state, "managed_outputs", None)
    try:
        mutations = state.runtime_mutations
        if request.session_id and frame.run_id:
            mutations.mark_writer_running(request.session_id, frame.run_id, provider_id=frame.provider_id)
        result = deps.agent_run(AgentRequest(
            provider=provider,
            project=Path(project),
            task=request.task,
            max_turns=request.max_turns,
            on_event=lambda event: record_planning_event(deps, frame, work, event),
            on_shell_request=None,
            stop_flag=stop_flag_for(state, frame.run_id),
            fresh_chat=frame.fresh_chat,
            change_tracker=None,
            conversation=frame.conversation,
            provider_id=frame.provider_id,
            handoff=frame.handoff,
            project_facts=project_context.verified_facts,
            research_context=project_context.research_context,
            project_map=project_context.project_map,
            project_config_warnings=project_context.project_config_warnings,
            memory=state.memory,
            permission_profile="planning_readonly",
            trace_recorder=trace,
            session_id=request.session_id,
            run_id=frame.run_id,
            effect_scope="planning:1",
            runtime_mutations=mutations,
            durable=state.durable,
            workspace_revision_store=workspace_revisions,
            managed_outputs=managed_outputs,
            requested_capabilities=request.requested_capabilities,
            strict_research=request.strict_research,
            project_changes_required=request.project_changes_required,
            task_policy=entry_policy,
            research_tools=research_tools,
        ))
    finally:
        close_research_tools(research_tools)
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
    event: dict[str, object] = {
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
    return ModeOutcome(event)


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
