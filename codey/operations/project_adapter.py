"""Coding workflow adapter for the shared model turn kernel.

The project workflow still owns review, repair and provider failover. This
adapter replaces only its model/tool turn loop, preserving its public
``AgentRequest`` and ``RunResult`` boundary for project callers.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from codey.agents.request import AgentRequest
from codey.operations.provider_session import ConversationProvider
from codey.operations.task_session import session_checks_passed
from codey.runtime.core.run_result import RunResult


def _project_context(request: AgentRequest) -> str:
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
        ghost_directive=request.ghost_directive,
        ghost_continuity=request.ghost_continuity,
        ghost_experiences=request.ghost_experiences,
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

        if not task_forbids_verification(request.task):
            lines = [
                f"- {str(item.command)[:240]} (cwd: {str(item.cwd)[:120]})"
                for item in request.verification_candidates[:5]
            ]
            candidate_text = "Trusted verification candidates after edits:\n" + "\n".join(lines)
    return f"{rendered.text}\n\n{candidate_text}" if candidate_text else rendered.text


def _open_fresh_chat(request: AgentRequest) -> bool:
    if not request.fresh_chat:
        return False
    request.provider.new_chat()
    return True


def _task_kind_and_policy(request: AgentRequest) -> tuple[str, Any]:
    from codey.agents.protocol import task_forbids_verification
    from codey.policies.task_policy import build_task_policy

    task_kind = "planning" if request.permission_profile == "planning_readonly" else "project"
    existing = getattr(request, "task_policy", None)
    if existing is not None and callable(getattr(existing, "allows", None)):
        return task_kind, existing
    policy = build_task_policy(
        SimpleNamespace(
            project=str(request.project),
            requested_capabilities=getattr(request, "requested_capabilities", ()),
            strict_research=bool(getattr(request, "strict_research", False) is True),
            project_changes_required=request.project_changes_required,
            denied_capabilities=("project.verify",) if task_forbids_verification(request.task) else (),
        ),
        task_kind=task_kind,
    )
    return task_kind, policy


def _require_write_permission(task_kind: str, policy: Any, request: AgentRequest) -> None:
    try:
        _requires = bool(getattr(request, "project_changes_required", False) is True)
    except Exception:
        _requires = False
    if _requires and task_kind in {"project", "hybrid"}:
        try:
            _allows = bool(policy.allows("project.write"))
        except Exception:
            _allows = False
        if not _allows:
            raise RuntimeError(
                "project_changes_required without project.write: task declares must-change "
                "but entry grants no write permission"
            )


def _wrap_provider_with_sink(request: AgentRequest, provider: Any) -> tuple[Any, Any]:
    intent_sink = None
    if request.runtime_mutations is not None and request.session_id and request.run_id:
        # Durable path requires an explicit provider_id; the empty string
        # fails instead of silently falling back to provider.name. The
        # provider.name fallback above is conversation-display only.
        if not str(request.provider_id or "").strip():
            raise ValueError("provider_id must not be empty when runtime_mutations are supplied")
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        intent_sink = KernelEffectSink(
            request.runtime_mutations, session_id=request.session_id,
            run_id=request.run_id, provider_id=request.provider_id,
            recovered_batch_id=request.recovered_tool_result_batch_id,
            managed_outputs=request.managed_outputs,
        )
        provider = KernelRecordedProvider(provider, intent_sink)
    if request.conversation is not None:
        provider = ConversationProvider(provider, request.conversation)
    return provider, intent_sink


def run(request: AgentRequest) -> RunResult:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession

    task_kind, policy = _task_kind_and_policy(request)
    _require_write_permission(task_kind, policy, request)
    if request.task_session is not None and request.task_session.policy != policy:
        raise ValueError("project continuation cannot replace task authorization")
    if request.runtime_mutations is not None and request.session_id and request.run_id:
        from codey.operations.recovery import record_entry_policy

        record_entry_policy(request.runtime_mutations, session_id=request.session_id,
                            run_id=request.run_id, policy=policy)
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
            "project", str(request.project),
        )
    session = request.task_session
    if session is None:
        session = TaskSession(
            policy=policy,
            task_kind=task_kind,
            project=str(request.project),
            max_turns=request.max_turns,
            task_text=request.task,
            handoff=request.handoff,
            project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
            coding_context_enabled=bool(getattr(request, "coding_context_enabled", True) is True),
            verification_candidates=request.verification_candidates,
            verification_candidate_loader=request.verification_candidate_loader,
        )
    shared = request.task_session is not None
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
    from codey.operations.kernel_session_recovery import restore_task_session

    delivered, _, resume_start, initial_results = restore_task_session(
        SimpleNamespace(
            run_id=request.run_id or "adhoc",
            recovered_tool_outcomes=request.recovered_tool_outcomes,
            settled_tool_outcomes=request.settled_tool_outcomes,
        ),
        session, effect_scope=effect_scope, research_ledger=getattr(request.research_tools, "ledger", None),
    )
    outcome = run_task_kernel(
        session,
        provider=provider,
        run_id=request.run_id,
        effect_scope=effect_scope,
        provider_id=request.provider_id,
        project_path=request.project,
        tool_fns=request.tool_fns,
        change_tracker=request.change_tracker,
        research_tools=request.research_tools,
        managed_outputs=request.managed_outputs,
        session_id=request.session_id,
        permission_profile=request.permission_profile,
        user_task=request.task,
        context_text=_project_context(request),
        stop_flag=request.stop_flag,
        stagnant_turns=request.stagnant_turns,
        delivered=delivered,
        intent_sink=intent_sink,
        workspace_revision_store=request.workspace_revision_store,
        workspace_ignored_paths=request.workspace_ignored_paths,
        trace_recorder=request.trace_recorder,
        on_event=request.on_event,
        on_shell_request=request.on_shell_request,
        propagate_provider_failure=True,
        start_turn=resume_start,
        initial_results=initial_results or None,
        completion_context={
            **(request.completion_context or {}),
            "run_id": request.run_id,
            "task": request.task,
            "question": request.task,
            "project": str(request.project),
        },
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
