"""Coding workflow adapter for the shared model turn kernel.

The project workflow still owns review, repair and provider failover. This
adapter replaces only its model/tool turn loop, preserving its public
``AgentRequest`` and ``RunResult`` boundary for project callers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from codey.agents.request import AgentRequest
from codey.ghost.payload_types import object_fields
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.project_experiences import validated_project_experiences
from codey.operations.project_goals import validated_project_goal_sources
from codey.operations.provider_session import ConversationProvider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
)
from codey.operations.task_session import TaskSession, session_checks_passed
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.run_result import RunResult
from codey.workspace.context_admission import GhostContextSource


@dataclass(frozen=True)
class _ProjectPolicyInput:
    project: str
    requested_capabilities: tuple[str, ...]
    strict_research: bool
    project_changes_required: bool
    denied_capabilities: tuple[str, ...]


def _project_context(request: AgentRequest, *, experiences: str = "") -> str:
    from codey.agents.context import build_agent_context, load_project_instructions
    from codey.agents.tools import DEFAULT_TOOL_FNS
    from codey.policies.permissions import profile_for_name
    from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
    from codey.workspace.context_epoch import context_epoch_id

    tool_fns = request.tool_fns or DEFAULT_TOOL_FNS
    rendered = build_agent_context(
        project=request.project,
        request_text=request.task,
        system_prompt_text="",
        profile=profile_for_name(request.permission_profile),
        list_directory=tool_fns.list_directory,
        project_instructions=load_project_instructions(request.project),
        project_facts=request.project_facts,
        research_context=request.research_context,
        project_map=request.project_map,
        project_config_warnings=request.project_config_warnings,
        work_checkpoint=request.work_checkpoint,
        ghost_directive=request.ghost_directive if request.admit_hints else "",
        ghost_continuity=request.ghost_continuity if request.admit_hints else "",
        ghost_experiences=experiences,
        completion_repair_context=request.completion_repair_context,
    )
    trace = FailOpenPromptTrace(request.trace_recorder)
    epoch = context_epoch_id(rendered.text)
    trace.call("record_permission_profile", request.permission_profile, phase="writer")
    for section in rendered.sections:
        trace.record_section(replace(section, epoch_id=epoch))
    trace.call("record_context_sources", rendered.sources, epoch_id=epoch)
    if request.completion_repair_context_payload and any(
        source.key == "completion_repair_context" for source in rendered.sources
    ):
        trace.call("record_completion_repair_context",
                   request.completion_repair_context_payload, epoch_id=epoch)
    candidate_text = ""
    if request.permission_profile == "coding_writer" and request.verification_candidates:
        from codey.agents.protocol import task_forbids_verification
        from codey.completion.verification_policy import render_verification_guidance

        if not task_forbids_verification(request.task):
            candidate_text = render_verification_guidance(request.verification_candidates)
    return f"{rendered.text}\n\n{candidate_text}" if candidate_text else rendered.text


def _project_ghost_sources(request: AgentRequest) -> tuple[GhostContextSource, ...]:
    from codey.policies.permissions import allows_context_source, profile_for_name
    from codey.workspace.context_admission import request_scope, sources_from_values

    profile = profile_for_name(request.permission_profile)
    if not request.admit_hints:
        return ()
    return (*sources_from_values(
        directive=(request.ghost_directive
                   if allows_context_source(profile, "ghost_directive") else ""),
        continuity=(request.ghost_continuity
                    if allows_context_source(profile, "ghost_continuity") else ""),
        experiences=(validated_project_experiences(request)
                     if allows_context_source(profile, "ghost_experiences") else ""),
        scope=request_scope(request.project, request.session_id),
    ), *validated_project_goal_sources(request))


def _open_fresh_chat(request: AgentRequest) -> bool:
    window_action = request.window_action
    if window_action is not None:
        from codey.operations.conversation_window import OpenWindow, activate_window

        activate_window(request.provider, window_action)
        return isinstance(window_action, OpenWindow)
    if not request.fresh_chat:
        return False
    request.provider.new_chat()
    return True


def _request_session(request: AgentRequest) -> TaskSession | None:
    session: object = request.task_session
    if session is not None and not isinstance(session, TaskSession):
        raise TypeError("project continuation requires TaskSession")
    return session


def _task_kind_and_policy(request: AgentRequest) -> tuple[str, TaskPolicy]:
    from codey.agents.protocol import task_forbids_verification
    from codey.policies.task_policy import build_task_policy

    existing = request.task_policy
    if existing is not None and not isinstance(existing, TaskPolicy):
        raise TypeError("project authorization requires TaskPolicy")
    # A read-only writer continuation narrows tool permissions for this
    # attempt; it must retain the parent project's task identity and
    # conversation mode. Standalone planning requests have no project-write
    # grant in their entry policy and remain planning tasks.
    session = _request_session(request)
    inherited_kind = session.task_kind.strip().lower() if session is not None else ""
    if inherited_kind in {"project", "hybrid", "research", "planning"}:
        task_kind = inherited_kind
    elif request.permission_profile == "planning_readonly":
        task_kind = (
            "project"
            if existing is not None and existing.allows("project.write")
            else "planning"
        )
    else:
        task_kind = "project"
    if existing is not None:
        return task_kind, existing
    policy = build_task_policy(
        _ProjectPolicyInput(
            project=str(request.project),
            requested_capabilities=request.requested_capabilities,
            strict_research=request.strict_research is True,
            project_changes_required=request.project_changes_required,
            denied_capabilities=("project.verify",) if task_forbids_verification(request.task) else (),
        ),
        task_kind=task_kind,
    )
    return task_kind, policy


def _require_write_permission(task_kind: str, policy: TaskPolicy, request: AgentRequest) -> None:
    if (request.project_changes_required is True and task_kind in {"project", "hybrid"}
            and not policy.allows("project.write")):
        raise RuntimeError(
            "project_changes_required without project.write: task declares must-change "
            "but entry grants no write permission"
        )


def _wrap_provider_with_sink(request: AgentRequest, provider: object) -> tuple[object, KernelEffectSink | None]:
    intent_sink: KernelEffectSink | None = None
    if request.runtime_mutations is not None and request.session_id and request.run_id:
        # Durable path requires an explicit provider_id; the empty string
        # fails instead of silently falling back to provider.name. The
        # provider.name fallback above is conversation-display only.
        if not (request.provider_id or "").strip():
            raise ValueError("provider_id must not be empty when runtime_mutations are supplied")
        intent_sink = KernelEffectSink(
            request.runtime_mutations, session_id=request.session_id,
            run_id=request.run_id, provider_id=request.provider_id,
            managed_outputs=request.managed_outputs,
            durable=request.durable,
        )
    if request.conversation is not None:
        provider = ConversationProvider(provider, request.conversation)
    return provider, intent_sink


def run(request: AgentRequest) -> RunResult:
    from codey.operations.task_loop import run_task_kernel
    from codey.workspace.context_admission import request_scope

    task_kind, policy = _task_kind_and_policy(request)
    _require_write_permission(task_kind, policy, request)
    session = _request_session(request)
    if session is not None and session.policy != policy:
        raise ValueError("project continuation cannot replace task authorization")
    if request.runtime_mutations is not None and request.session_id and request.run_id:
        from codey.operations.recovery import durable_task_for, record_entry_policy

        record_entry_policy(request.runtime_mutations, session_id=request.session_id,
                            run_id=request.run_id, policy=policy,
                            durable=durable_task_for(request.durable, request.run_id))
    provider, intent_sink = _wrap_provider_with_sink(request, request.provider)
    if policy.allows("project.write"):
        request.project.mkdir(parents=True, exist_ok=True)
    elif not request.project.is_dir():
        raise RuntimeError("associated project directory does not exist")
    opened_fresh_chat = _open_fresh_chat(request)
    if request.conversation is not None and opened_fresh_chat:
        # Display-only label via the centralized helper; durable paths
        # require an explicit provider_id (see below) and never use this.
        from codey.providers.catalog import display_provider_name

        request.conversation.begin_window(
            display_provider_name(request.provider_id, request.provider),
            "project", str(request.project.expanduser().resolve()),
        )
    shared = session is not None
    if session is None:
        from codey.task.web_intent import explicit_web_intent

        session = TaskSession(
            policy=policy,
            task_kind=task_kind,
            project=str(request.project),
            max_turns=request.max_turns,
            task_text=request.task,
            required_source_urls=explicit_web_intent(request.task).urls,
            handoff=request.handoff,
            project_changes_required=request.project_changes_required is True,
            coding_context_enabled=request.coding_context_enabled is True,
            verification_candidates=request.verification_candidates,
            verification_candidate_loader=request.verification_candidate_loader,
        )
    if not shared and request.workspace_revision_store is not None:
        current = request.workspace_revision_store.current_state(
            str(request.project), ignored_paths=request.workspace_ignored_paths,
        )
        session.set_workspace_state(current.revision, current.fingerprint)
    session.max_turns = request.max_turns
    session.handoff = request.handoff
    session.verification_candidates = request.verification_candidates
    session.verification_candidate_loader = request.verification_candidate_loader
    if not shared:
        from codey.agents.protocol import task_forbids_verification

        session.verification_forbidden = ("project.verify" in policy.denied_capabilities or
                                              task_forbids_verification(request.task))
    effect_scope = request.effect_scope or ("planning:1" if task_kind == "planning" else "writer:1")
    ghost_sources = _project_ghost_sources(request)
    experiences = next((source.text for source in ghost_sources if source.key == "ghost_experiences"), "")
    completion_context: object = request.completion_context
    if completion_context is None:
        completion_context = dict[str, object]()
    if not object_fields(completion_context):
        raise TypeError("project completion context requires an object with string keys")

    def sources_still_valid(actual: tuple[GhostContextSource, ...]) -> bool:
        # Recovery can replace the transport's context/sources. Validate that
        # actual tuple, rather than authorizing a different restored request
        # through the new entry's otherwise valid projection.
        return request.admit_hints and actual == ghost_sources and _project_ghost_sources(request) == actual

    goal_context = "\n\n".join(source.text for source in ghost_sources
                               if source.key in {"ghost_project_goal_history", "ghost_confirmed_project_goal"})
    context_text = _project_context(request, experiences=experiences)
    if goal_context:
        context_text = f"{context_text}\n\n{goal_context}"
    outcome = run_task_kernel(
        session,
        request=KernelRunRequestBuilder(
                provider=provider,
                run_id=request.run_id,
                effect_scope=effect_scope,
                provider_id=request.provider_id,
                user_task=request.task,
                context_text=context_text,
                context_scope=request_scope(request.project, request.session_id),
                ghost_sources=ghost_sources,
                admit_hints=request.admit_hints,
                attempt_validate_ghost_sources=sources_still_valid,
                stop_flag=request.stop_flag,
                stagnant_turns=request.stagnant_turns,
                task_guidance=task_guidance_for_policy(session.policy, task_kind=session.task_kind),
            ).build(execution=KernelExecutionDeps(
                intent_sink=intent_sink,
                project_path=request.project,
                tool_fns=request.tool_fns,
                change_tracker=request.change_tracker,
                research_tools=request.research_tools,
                permission_profile=request.permission_profile,
                workspace_revision_store=request.workspace_revision_store,
                workspace_ignored_paths=request.workspace_ignored_paths,
            ), observation=KernelObservationDeps(
                on_boundary=durable_generation_boundary_callback(
                    durable=request.durable,
                    session_id=request.session_id,
                    task_id=request.run_id,
                    sink=intent_sink,
                ),
                completion_context={
                    **completion_context,
                    "run_id": request.run_id,
                    "task": request.task,
                    "question": request.task,
                    "project": str(request.project),
                },
                on_event=request.on_event,
                on_shell_request=request.on_shell_request,
                propagate_provider_failure=True,
                trace_recorder=request.trace_recorder,
            )),
    )
    result = RunResult(
        summary=outcome.summary,
        stop_reason=outcome.stop_reason,
        turns=outcome.turns,
        checks_passed=session_checks_passed(session, outcome.proof),
        changed=bool(session.edited_files),
        checks_ran=bool(session.verifications),
        proof=outcome.proof,
        facts=session,
        delivery=outcome.delivery,
    )
    if request.conversation is not None:
        from codey.agents.handoff import ConversationSnapshot
        from codey.providers.catalog import display_provider_name

        prior = request.conversation.snapshot
        request.conversation.update_snapshot(ConversationSnapshot(
            mode=task_kind, goal=prior.goal or request.task, project=str(request.project),
            provider_id=display_provider_name(request.provider_id, request.provider),
            changed_files=tuple(sorted({*prior.changed_files, *session.edited_files})),
            checks_passed=result.checks_passed, summary=result.summary,
            blocker="" if result.stop_reason == "done" else result.summary,
            conversation_summary=prior.conversation_summary,
        ))
    return result


__all__ = ["run"]
