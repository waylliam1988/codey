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


def _recovered_result_for_row(row: Any) -> Any:
    from codey.operations.kernel_recovery import RecoveryFailed
    from codey.operations.kernel_result import build_recovered_tool_result

    # Preserve the full kernel-owned recovery metadata (audit with the
    # trusted workspace identity, presentation/canonical/truncated) so a
    # recovered edit keeps its (revision, fingerprint) and hooks adopt
    # without a second bump. Building audit={"changed": ...} only would
    # drop the trusted identity. Any failure raises RecoveryFailed: the
    # caller must stop instead of consuming a half-recovered success.
    try:
        outcome_audit = dict(getattr(row.outcome, "audit", {}) or {})
    except Exception as exc:
        raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
    try:
        if row.call.name == "edit" and "changed" not in outcome_audit:
            outcome_audit["changed"] = bool(row.outcome.changed)
    except Exception as exc:
        raise RecoveryFailed(f"recovered changed unreadable: {exc}") from exc
    try:
        return build_recovered_tool_result(
            row.call,
            model_text=row.outcome.model_text,
            truncated=bool(getattr(row.outcome, "truncated", False)),
            presentation=dict(getattr(row.outcome, "presentation", {}) or {}),
            audit=outcome_audit,
            canonical=dict(getattr(row.outcome, "canonical", {}) or {}),
        )
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovered result rebuild failed: {exc}") from exc


def _open_fresh_chat(request: AgentRequest) -> bool:
    if not request.fresh_chat:
        return False
    from codey.runtime.core import cancellation
    from codey.runtime.observe.events import RunEvent

    try:
        request.provider.new_chat()
        return True
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        if request.strict_fresh_chat:
            raise
        if request.on_event is not None:
            request.on_event(RunEvent.status(
                f"[agent] could not open new chat: {exc}; reusing current tab"
            ))
        return False


def _task_kind_and_policy(request: AgentRequest) -> tuple[str, Any]:
    from codey.policies.task_policy import build_task_policy

    task_kind = "planning" if request.permission_profile == "planning_readonly" else "project"
    policy = build_task_policy(
        SimpleNamespace(
            project=str(request.project),
            requested_capabilities=getattr(request, "requested_capabilities", ()),
            strict_research=False,
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
        )
        provider = KernelRecordedProvider(provider, intent_sink)
    if request.conversation is not None:
        provider = _ConversationProvider(provider, request.conversation)
    return provider, intent_sink


def run(request: AgentRequest) -> RunResult:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession, turn_effect_id

    if request.permission_profile == "coding_writer":
        request.project.mkdir(parents=True, exist_ok=True)
    opened_fresh_chat = _open_fresh_chat(request)
    if request.conversation is not None and opened_fresh_chat:
        # Display-only label via the centralized helper; durable paths
        # require an explicit provider_id (see below) and never use this.
        from codey.providers.catalog import display_provider_name

        request.conversation.begin_window(
            display_provider_name(request.provider_id, request.provider),
            "project", str(request.project),
        )
    task_kind, policy = _task_kind_and_policy(request)
    _require_write_permission(task_kind, policy, request)
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
    from codey.operations.kernel_recovery import RecoveryFailed as _RecoveryFailed

    try:
        for row in list(request.recovered_tool_outcomes or ()):
            if getattr(row, "call", None) is None or getattr(row, "outcome", None) is None:
                raise _RecoveryFailed("malformed recovered row: missing call/outcome")
            int(getattr(row, "turn", None))
            int(getattr(row, "tool_index", None))
        recovered_sorted = sorted(
            list(request.recovered_tool_outcomes or ()),
            key=lambda item: (int(item.turn), int(item.tool_index)),
        )
    except _RecoveryFailed:
        raise
    except Exception as exc:
        raise _RecoveryFailed(f"malformed recovered rows: {exc}") from exc
    delivered: dict[str, Any] = {}
    try:
        from codey.operations.kernel_facts import record_facts_for_result as _record_facts

        for row in recovered_sorted:
            result = _recovered_result_for_row(row)
            delivered[turn_effect_id(f"{request.run_id or 'adhoc'}:{effect_scope}",
                                     row.turn, row.tool_index)] = result
            _record_facts(session, row.call, result, ok=row.outcome.ok,
                          exit_code=row.outcome.exit_code)
    except _RecoveryFailed:
        raise
    except Exception as exc:
        raise _RecoveryFailed(f"recovered facts replay failed: {exc}") from exc
    # Recovery-first: deliver the original batch before any new model call,
    # and resume after the max recovered turn so identities never collide.
    resume_start = 1
    initial_results: list[Any] = []
    if recovered_sorted:
        try:
            resume_start = max(int(getattr(r, "turn", None)) for r in recovered_sorted) + 1
            resume_start = max(1, resume_start)
        except Exception as exc:
            raise _RecoveryFailed(f"recovered resume turn unreadable: {exc}") from exc
        for row in recovered_sorted:
            initial_results.append(delivered[turn_effect_id(
                f"{request.run_id or 'adhoc'}:{effect_scope}", row.turn, row.tool_index)])
    provider, intent_sink = _wrap_provider_with_sink(request, request.provider)
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
