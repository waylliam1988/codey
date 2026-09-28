"""Coding workflow adapter for the shared model turn kernel.

The project workflow still owns review, repair and provider failover. This
adapter replaces only its model/tool turn loop, preserving its public
``AgentRequest`` and ``RunResult`` boundary during migration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from codey.agents.request import AgentRequest
from codey.runtime.core.run_result import RunResult


class _ConversationProvider:
    def __init__(self, provider: Any, conversation: Any) -> None:
        self.provider = provider
        self.conversation = conversation

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.provider, name)
        if name in {"send_turn", "send_tool_results"} and callable(value):
            return lambda *args, **kwargs: self._send(name, *args, **kwargs)
        return value

    def send(self, *args: Any, **kwargs: Any) -> Any:
        return self._send("send", *args, **kwargs)

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        reply = getattr(self.provider, name)(*args, **kwargs)
        prompt = str(args[0]) if name in {"send", "send_turn"} and args else json.dumps(
            args[0] if args else (), ensure_ascii=False, default=str,
        )
        answer = reply if isinstance(reply, str) else str(getattr(reply, "text", "") or reply)
        self.conversation.record_exchange(prompt, answer)
        return reply


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
        from codey.agents.verification_driver import forbids_verification

        if not forbids_verification(request.task):
            lines = [
                f"- {str(item.command)[:240]} (cwd: {str(item.cwd)[:120]})"
                for item in request.verification_candidates[:5]
            ]
            candidate_text = "Trusted verification candidates after edits:\n" + "\n".join(lines)
    return f"{rendered.text}\n\n{candidate_text}" if candidate_text else rendered.text


def run(request: AgentRequest) -> RunResult:
    from codey.operations.kernel_execution import record_facts_for_result
    from codey.operations.task_loop import TaskSession, run_task_kernel, turn_effect_id
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolResult

    if request.permission_profile == "coding_writer":
        request.project.mkdir(parents=True, exist_ok=True)
    opened_fresh_chat = False
    if request.fresh_chat:
        from codey.runtime.core import cancellation
        from codey.runtime.observe.events import RunEvent

        try:
            request.provider.new_chat()
            opened_fresh_chat = True
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            if request.strict_fresh_chat:
                raise
            if request.on_event is not None:
                request.on_event(RunEvent.status(
                    f"[agent] could not open new chat: {exc}; reusing current tab"
                ))
    if request.conversation is not None and opened_fresh_chat:
        request.conversation.begin_window(
            request.provider_id or getattr(request.provider, "name", ""),
            "project", str(request.project),
        )
    task_kind = "planning" if request.permission_profile == "planning_readonly" else "project"
    policy = build_task_policy(
        SimpleNamespace(
            project=str(request.project),
            requested_capabilities=getattr(request, "requested_capabilities", ()),
            strict_research=False,
        ),
        task_kind=task_kind,
    )
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
    session = TaskSession(
        policy=policy,
        task_kind=task_kind,
        project=str(request.project),
        max_turns=request.max_turns,
        task_text=request.task,
        handoff=request.handoff,
        project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
        coding_context_enabled=bool(getattr(request, "coding_context_enabled", True) is True),
    )
    effect_scope = request.effect_scope or ("planning:1" if task_kind == "planning" else "writer:1")
    delivered: dict[str, ToolResult] = {}
    recovered_sorted = sorted(request.recovered_tool_outcomes, key=lambda item: (item.turn, item.tool_index))
    for row in recovered_sorted:
        result = ToolResult(call=row.call, model_text=row.outcome.model_text,
                            audit={"changed": bool(row.outcome.changed)} if row.call.name == "edit" else {})
        delivered[turn_effect_id(f"{request.run_id or 'adhoc'}:{effect_scope}",
                                 row.turn, row.tool_index)] = result
        record_facts_for_result(session, row.call, result, ok=row.outcome.ok,
                                 exit_code=row.outcome.exit_code)
    # Recovery-first: deliver the original batch before any new model call,
    # and resume after the max recovered turn so identities never collide.
    resume_start = 1
    initial_results: list[ToolResult] = []
    if recovered_sorted:
        try:
            resume_start = max(int(getattr(r, "turn", 1) or 1) for r in recovered_sorted) + 1
        except Exception:
            resume_start = 1
        for row in recovered_sorted:
            initial_results.append(ToolResult(call=row.call, model_text=row.outcome.model_text,
                                              audit={"changed": bool(row.outcome.changed)}
                                              if row.call.name == "edit" else {}))
    provider = request.provider
    intent_sink = None
    if request.runtime_mutations is not None and request.session_id and request.run_id:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        intent_sink = KernelEffectSink(
            request.runtime_mutations, session_id=request.session_id,
            run_id=request.run_id, provider_id=request.provider_id,
            recovered_batch_id=request.recovered_tool_result_batch_id,
        )
        provider = KernelRecordedProvider(provider, intent_sink)
    if request.conversation is not None:
        provider = _ConversationProvider(provider, request.conversation)
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
        on_event=request.on_event,
        on_shell_request=request.on_shell_request,
        propagate_provider_failure=True,
        start_turn=resume_start,
        initial_results=initial_results or None,
    )
    return RunResult(
        summary=outcome.summary,
        stop_reason=outcome.stop_reason,
        turns=outcome.turns,
        checks_passed=bool(session.verifications and session.verifications[-1].get("passed")),
        changed=bool(session.edited_files),
        checks_ran=bool(session.verifications),
    )


__all__ = ["run"]
