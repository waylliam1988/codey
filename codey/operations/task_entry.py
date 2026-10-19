"""Single task entry: runtime submission + mode dispatch (one import point).

Runtime entry ``run_task_submission`` and mode entry ``run_task_mode`` converge
here. Hybrid and auto continuations use the shared entry session; project,
explicit Research and planning add their workflow strategies around the same
kernel. All production tool calls converge in
``run_task_kernel``/``execute_turn``; ``done`` converges in the single
``completion_gate``.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, cast

from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.conversation_window import WindowActivationError, activate_window, replayed_result_ids
from codey.operations.kernel_session_recovery import restore_task_session
from codey.operations.result import ModeOutcome
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.task.kind import ui_mode
from codey.task.model import TaskSubmission

_TASK_KINDS = frozenset({"project", "research", "hybrid", "planning", "planning_readonly", "readonly"})


@dataclass(frozen=True)
class FreshSessionOutcome:
    ok: bool
    error: str = ""


def _durable_boundary_callback(
    deps: Any,
    session_id: str,
    task_id: str = "",
) -> Callable[[str], str | None] | None:
    """Build the narrow callback used by the kernel generation boundary."""
    durable = getattr(getattr(deps, "state", None), "durable", None)
    if durable is None or not callable(getattr(durable, "commit_generation_boundary", None)):
        return None
    if not session_id or not task_id or not durable.has_conversation(session_id):
        return None

    def place_boundary(boundary: str) -> str | None:
        placed = durable.commit_generation_boundary(
            session_id, task_id=task_id, at=boundary,
        )
        texts: list[str] = []
        for submission in placed:
            content = getattr(submission, "content", None)
            if not isinstance(content, Mapping):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        return "\n\n".join(texts) or None

    return place_boundary


def _open_fresh_session(
    conversation: Any, provider: Any, *, provider_id: str, kind: str, project: str = "",
) -> FreshSessionOutcome:
    """Open a fresh provider session; reset the window only on success.

    重置失败一律返回失败（调用方停止并报 provider failure），绝不吞掉
    异常后继续向旧会话发送；非成功路径绝不触碰原窗口与累计预算。
    """
    try:
        new_chat = getattr(provider, "new_chat", None)
        if not callable(new_chat):
            return FreshSessionOutcome(ok=False, error="provider has no new chat session")
        new_chat()
    except Exception as exc:
        return FreshSessionOutcome(ok=False, error=f"new chat failed: {exc}")
    try:
        conversation.begin_window(provider_id, kind, project)
    except Exception as exc:
        return FreshSessionOutcome(ok=False, error=f"conversation reset failed: {exc}")
    return FreshSessionOutcome(ok=True)


def _task_kind(task_kind: object) -> str:
    kind = str(task_kind or "").strip().lower()
    if kind in _TASK_KINDS:
        return "planning" if kind in {"planning_readonly", "readonly"} else kind
    return "chat" if kind == "auto" else kind


def build_task_policy_for_entry(request: Any, task_kind: object) -> Any:
    from codey.agents.protocol import task_forbids_verification
    from codey.policies.task_policy import policy_for_dispatch

    kind = _task_kind(task_kind)
    strict = kind == "research" or bool(getattr(request, "strict_research", False) is True)
    policy = policy_for_dispatch(request, kind, strict_research=strict)
    if policy is None:
        raise RuntimeError("task policy configuration failed")
    if task_forbids_verification(str(getattr(request, "task", "") or "")):
        policy = replace(policy, grants=policy.grants - {"project.verify"},
                         denied_capabilities=policy.denied_capabilities | {"project.verify"},
                         source=policy.source + ";verification:forbidden")
    return policy


def start_task_session(frame: RunFrame, work: RunWork, policy: Any, kind: str) -> Any:
    from codey.operations.task_session import TaskSession

    existing = getattr(frame, "entry_session", None)
    if existing is not None:
        if existing.policy != policy:
            raise ValueError("task continuation cannot replace authorization")
        return existing
    request = frame.request
    session = TaskSession(
        policy=policy,
        task_kind=_task_kind(kind),
        project=frame.project_text,
        max_turns=max(1, int(getattr(request, "max_turns", 8) or 8)),
        task_text=request.task,
        handoff=str(getattr(frame, "handoff", "") or ""),
        project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
        coding_context_enabled=bool(getattr(request, "coding_context_enabled", True) is True),
        verification_forbidden=("project.verify" in policy.denied_capabilities),
    )
    ev = getattr(work, "evidence", None)
    if ev is not None:
        session.set_workspace_state(ev.workspace_revision, ev.workspace_fingerprint)
    # The entry owns the common fact view: stash it on the frame for strategy
    # phases so project/research/planning projections read the same session
    # instead of forking their own facts.
    frame.entry_session = session
    return session


def _entry_policy_with_recovery(frame: RunFrame, deps: Any, kind: str) -> Any:
    request = frame.request
    from codey.operations.recovery import durable_task_for, policy_from_durable_task

    durable = durable_task_for(getattr(getattr(deps, "state", None), "durable", None), frame.run_id)
    incoming = getattr(frame, "entry_policy", None)
    if incoming is None:
        incoming = build_task_policy_for_entry(replace(request, project=frame.project_text or None), kind)
    if getattr(request, "previous_run_id", ""):
        if getattr(frame, "entry_policy", None) is None:
            raise RuntimeError("original task authorization unavailable for approval continuation")
        return incoming
    recovered = bool(getattr(frame, "recovered_tool_outcomes", ()) or getattr(frame, "settled_tool_outcomes", ()))
    if not recovered:
        return incoming
    # A resumed run must use the policy recorded at its original entry. Never
    # replace a missing or malformed persisted policy with the new request's
    # capabilities; missing grants or requirements stop recovery.
    return policy_from_durable_task(durable, task_id=frame.run_id)


def _entry_check_conflict(policy: Any, request: Any, kind: str) -> None:
    try:
        requires = (getattr(request, "project_changes_required", False) is True
                    or "project_changes_required" in policy.required_checks)
    except Exception:
        requires = False
    if requires and kind in {"project", "hybrid"}:
        try:
            allows = bool(policy.allows("project.write"))
        except Exception:
            allows = False
        if not allows:
            raise RuntimeError(
                "project_changes_required without project.write: task declares must-change "
                "but entry grants no write permission"
            )


def _entry_executors(frame: RunFrame, deps: Any, policy: Any) -> tuple[Any | None, Any, Any | None]:
    request = frame.request
    project_path: Path | None = None
    if frame.project_text:
        candidate = Path(frame.project_text).expanduser()
        if policy.allows("project.write"):
            candidate.mkdir(parents=True, exist_ok=True)
        if candidate.is_dir():
            project_path = candidate.resolve()
    tool_fns = None
    if project_path is not None:
        from codey.agents.tools import DEFAULT_TOOL_FNS

        tool_fns = DEFAULT_TOOL_FNS
    research_tools = None
    if any(policy.allows(grant) for grant in ("web.read", "knowledge.read", "knowledge.write", "knowledge.link")):
        from codey.operations.task_execution import build_research_tools

        research_tools = build_research_tools(
            deps, session_id=request.session_id, project=frame.project_text)
    return project_path, tool_fns, research_tools


def _entry_provider_sink(frame: RunFrame, deps: Any, kind: str) -> tuple[Any, Any, Any]:
    request = frame.request
    intent_sink = None
    active_provider = frame.provider
    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is not None and request.session_id and frame.run_id:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        mutations.mark_writer_running(request.session_id, frame.run_id,
                                      provider_id=frame.provider_id)
        from codey.operations.task_effects import admitted_durable

        intent_sink = KernelEffectSink(
            mutations, session_id=request.session_id, run_id=frame.run_id,
            provider_id=frame.provider_id,
            phase="research" if kind == "research" else "writer",
            recovered_batch_id=str(getattr(frame, "recovered_tool_result_batch_id", "") or ""),
            managed_outputs=getattr(deps, "managed_outputs", None),
            durable=admitted_durable(deps.state, frame.run_id),
        )
        active_provider = KernelRecordedProvider(active_provider, intent_sink)
    if getattr(frame, "conversation", None) is not None:
        from codey.operations.provider_session import ConversationProvider

        active_provider = ConversationProvider(active_provider, frame.conversation)
    stop_flag = getattr(getattr(deps, "state", None), "run_registry", None)
    stop_flag = getattr(stop_flag, "stop_flag", None)
    return active_provider, intent_sink, stop_flag


def _entry_project_tracker(project_path: Path | None, deps: Any, policy: Any) -> Any:
    if project_path is None or not policy.allows("project.write"):
        return None
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.workspace.changes import is_git_repository

    try:
        tracker = deps.state.change_tracker_for(project_path, persistent=not is_git_repository(project_path))
        if tracker is None:
            raise ValueError("project change tracker is missing")
        return tracker
    except Exception as exc:
        raise RecoveryFailed(f"project change tracking unavailable: {exc}") from exc


def _entry_project_receipt(frame: RunFrame, work: RunWork, hooks: RunHooks, deps: Any,
                           session: Any, result: Any, tracker: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from codey.operations.task_session import session_checks_passed
    from codey.runs.receipt import build_task_receipt

    if tracker is None or not session.edited_files:
        return {"display": {"summary": str(result.summary or "")[:2000]}}, None
    changes = deps.collect_changes(frame.project_text, tracker)
    if not isinstance(changes, dict) or changes.get("ok") is not True:
        raise RuntimeError("project change collection failed; cannot publish its receipt")
    checks_passed = session_checks_passed(session, result.proof)
    receipt = build_task_receipt(changes, proof=result.proof, checks_passed=checks_passed).to_dict()
    if work.ledger is not None:
        hooks.append_ledger(lambda ledger: ledger.append_changes_collected(
            changes, checks_passed=checks_passed, receipt=receipt))
    payload = {"changed_count": changes.get("changed_count", 0),
               "files": changes.get("files", [])[:3], "mode": changes.get("mode"),
               "project": frame.project_text}
    return receipt, payload


def _entry_context(frame: RunFrame, deps: Any, policy: Any) -> str:
    from codey.agents.request import AgentRequest
    from codey.operations.ghost_context import ghost_continuity, ghost_directive, ghost_experiences
    from codey.operations.project_adapter import _project_context
    from codey.operations.prompting import join_local_contexts, record_local_context_trace

    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    scope = request.project or ""
    directive = ghost_directive(deps.state, session_id=request.session_id, project=scope)
    continuity = ghost_continuity(deps.state, session_id=request.session_id, project=scope)
    experiences = ghost_experiences(deps.state, session_id=request.session_id, project=scope,
                                     query=request.task, exclude_run_id=frame.run_id)
    record_local_context_trace(getattr(frame, "trace", None), directive, continuity)
    if not policy.allows("project.read"):
        return str(join_local_contexts(directive.text, continuity.text, experiences))
    workspace_context = _project_context(AgentRequest(
        provider=frame.provider, project=Path(frame.project_text), task=request.task,
        permission_profile="coding_writer" if policy.allows("project.write") else "planning_readonly",
        trace_recorder=getattr(frame, "trace", None),
    ))
    return str(join_local_contexts(directive.text, continuity.text, experiences, workspace_context))


def _workspace_writer(frame: RunFrame, state: Any, stack: ExitStack) -> Any:
    acquired = False

    def ensure() -> None:
        nonlocal acquired
        if acquired:
            return
        from codey.workspace.changes import ProjectWriteBusy, is_git_repository

        if is_git_repository(frame.project_text):
            return
        if not state.acquire_project_writer(frame.project_text):
            raise ProjectWriteBusy("another task is writing this workspace")
        acquired = True
        stack.callback(state.release_project_writer, frame.project_text)

    return ensure


def run_entry_kernel(frame: RunFrame, work: RunWork, hooks: RunHooks, deps: Any,
                     *, task_kind: str = "", config_result: Any = None, continuation_followup: str = "") -> ModeOutcome:
    from codey.operations.task_execution import close_research_tools

    try:
        with ExitStack() as writer_stack:
            return _run_entry_kernel(frame, work, hooks, deps,
                                     task_kind=task_kind, config_result=config_result,
                                     continuation_followup=continuation_followup, writer_stack=writer_stack)
    finally:
        close_research_tools(getattr(frame, "entry_research_tools", None))


def _run_entry_kernel(  # noqa: PLR0915
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: Any,
    *,
    task_kind: str = "",
    config_result: Any = None,
    continuation_followup: str = "",
    writer_stack: ExitStack,
) -> ModeOutcome:
    from codey.operations.task_loop import run_task_kernel
    request = frame.request
    kind = _task_kind(task_kind or frame.task_kind)
    if not frame.project_text and kind == "chat":
        from codey.storage.local_store import DEFAULT_STATE_HOME
        from codey.workspace.session import session_workspace

        state_home = getattr(getattr(deps, "workspace_revisions", None), "state_home", None)
        state_home = state_home or getattr(deps.state, "state_home", None) or DEFAULT_STATE_HOME
        frame.project_text = str(session_workspace(state_home, request.session_id))
    policy = _entry_policy_with_recovery(frame, deps, kind)
    if policy is None:
        raise RuntimeError("task policy unavailable during recovery")
    _entry_check_conflict(policy, request, kind)
    session = start_task_session(frame, work, policy, kind)
    from codey.operations.recovery import durable_task_for, record_entry_policy

    record_entry_policy(
        getattr(deps, "runtime_mutations", None),
        session_id=request.session_id, run_id=frame.run_id, policy=policy,
        durable=durable_task_for(getattr(getattr(deps, "state", None), "durable", None), frame.run_id),
    )
    project_path, tool_fns, research_tools = _entry_executors(frame, deps, policy)
    previous_tools = getattr(frame, "entry_research_tools", None)
    if research_tools is not None and previous_tools is not None:
        research_tools.ledger = previous_tools.ledger
    frame.entry_research_tools = research_tools
    try:
        delivered, _, resume_start, initial_results = restore_task_session(
            frame, session, research_ledger=getattr(research_tools, "ledger", None),
        )
    except Exception as exc:
        reason = f"recovery failed: {exc}"
        return ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": reason,
            "stop_reason": "recovery_failure",
            "turns": 0,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": kind,
            "receipt": {"display": {"summary": reason[:2000]}},
        })
    # 会话生命周期：仅 fresh_chat 且 new_chat 成功后才开启新窗口（首发之前）；
    # 重置失败立即停止并报 provider failure，不向旧会话发送；继续会话时
    # 保留原窗口与累计预算。
    if bool(getattr(frame, "fresh_chat", False)):
        if frame.provider is None:
            return ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": "provider failed [Exception]: provider is not connected",
                "stop_reason": "provider_failure",
                "turns": 0,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": kind,
                "receipt": {"display": {"summary": "provider is not connected"}},
            })
        try:
            action = frame.window_action
            if action is not None:
                activate_window(frame.provider, action)
                frame.conversation.begin_window(frame.provider_id, kind, frame.project_text)
                fresh = FreshSessionOutcome(ok=True)
            else:
                fresh = _open_fresh_session(
                    frame.conversation, frame.provider,
                    provider_id=frame.provider_id, kind=kind, project=frame.project_text,
                )
        except WindowActivationError as exc:
            fresh = FreshSessionOutcome(ok=False, error=str(exc))
        if not fresh.ok:
            return ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": f"provider failed [Exception]: {fresh.error}",
                "stop_reason": "provider_failure",
                "turns": 0,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": kind,
                "receipt": {"display": {"summary": fresh.error[:2000]}},
            })
    active_provider, intent_sink, stop_flag = _entry_provider_sink(frame, deps, kind)
    context_text = _entry_context(frame, deps, policy)
    ensure_workspace_writer = _workspace_writer(frame, deps.state, writer_stack) if kind == "chat" else None
    try:
        ignored = tuple(getattr(getattr(config_result, "config", None), "ignored_paths", ()) or ())
    except Exception:
        ignored = ()
    try:
        tracker = _entry_project_tracker(project_path, deps, policy)
        initial_turn, frame.entry_initial_turn = frame.entry_initial_turn, None
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                task_guidance=task_guidance_for_policy(session.policy),
                transport=KernelTransportDeps(
                    provider=active_provider,
                    run_id=frame.run_id,
                    effect_scope="task",
                    provider_id=frame.provider_id,
                    user_task=request.task,
                    stop_flag=stop_flag,
                    delivered=delivered or None,
                    start_turn=max(resume_start, session.turn + 1) if continuation_followup else resume_start,
                    context_text="\n\n".join(part for part in (context_text, continuation_followup) if part),
                    initial_results=initial_results or None,
                    initial_turn=initial_turn,
                    window_ref=getattr(active_provider, "window_ref", None),
                    provider_session_changed=bool(getattr(frame, "provider_session_changed", False)),
                    replayed_result_ids=replayed_result_ids(frame.window_action),
                ),
                execution=KernelExecutionDeps(
                    executors={},
                    project_path=project_path,
                    tool_fns=tool_fns,
                    change_tracker=tracker,
                    research_tools=research_tools,
                    managed_outputs=getattr(deps, "managed_outputs", None),
                    session_id=request.session_id,
                    permission_profile="research" if kind == "research" else "coding_writer",
                    workspace_ignored_paths=ignored,
                    workspace_revision_store=getattr(deps, "workspace_revisions", None),
                    ensure_workspace_writer=ensure_workspace_writer,
                ),
                observation=KernelObservationDeps(
                    intent_sink=intent_sink,
                    on_event=hooks.on_event,
                    on_shell_request=hooks.on_shell_request,
                    completion_context={
                        "run_id": frame.run_id,
                        "task": request.task,
                        "question": request.task,
                        "project": frame.project_text,
                        "execution_evidence": work.evidence,
                        "analysis_run_payloads": work.analysis_run_payloads,
                        "research_ledger": getattr(research_tools, "ledger", None),
                    },
                    trace_recorder=getattr(frame, "trace", None),
                    on_boundary=_durable_boundary_callback(
                        deps, str(request.session_id or ""), frame.run_id,
                    ),
                ),
            ),
        )
    except Exception as exc:
        from codey.operations.kernel_errors import RecoveryFailed

        if not isinstance(exc, RecoveryFailed):
            raise
        reason = f"recovery failed: {exc}"
        return ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": reason,
            "stop_reason": "recovery_failure",
            "turns": 0,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": kind,
            "receipt": {"display": {"summary": reason[:2000]}},
        })
    # Stash the session + ledger for hybrid quality phases (same fact view).
    try:
        frame.entry_session = session
        frame.entry_research_tools = research_tools
    except Exception:
        pass
    # 生命周期收口：只更新同一会话视图的快照，不再无条件 begin_window
    #（继续会话保留原窗口与累计预算）。各轮 token 已由 provider 适配器
    # 逐轮记账，最终只更新任务摘要。
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        from dataclasses import replace as _replace2

        summary_text = (result.summary or "")
        snapshot = _replace2(
            frame.conversation.snapshot,
            mode=kind,
            goal=request.task,
            project=frame.project_text,
            provider_id=frame.provider_id,
            blocker="" if result.stop_reason == "done" else summary_text,
            latest_user=request.task,
            latest_reply=summary_text,
            summary=summary_text,
        )
        frame.conversation.update_snapshot(snapshot)
    summary = (result.summary or "")
    deps.state.set_provider_session(
        frame.provider_id,
        None if result.stop_reason in {"stopped", "provider_failure", "error"} else request.session_id,
    )
    receipt, changes = _entry_project_receipt(frame, work, hooks, deps, session, result, tracker)
    proof = getattr(result, "proof", None)
    if proof is not None:
        to_payload = getattr(proof, "to_payload", None)
        if callable(to_payload):
            with _contextlib.suppress(Exception):
                receipt["completion_proof"] = to_payload()
    event = {
        "type": "task_done",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "summary": summary,
        "stop_reason": result.stop_reason,
        "final_delivery": result.delivery,
        "turns": max(result.turns, session.turn),
        "max_turns": request.max_turns,
        "provider": frame.provider_id,
        "mode": ui_mode(kind, frame.project_text),
        "receipt": receipt,
    }
    if result.stop_reason == "provider_failure":
        from codey.providers.catalog import PROVIDER_LABELS
        from codey.providers.diagnostics import capture_provider_failure

        event["provider_failure"] = capture_provider_failure(
            model=PROVIDER_LABELS.get(frame.provider_id, frame.provider_id), action="task", page=None,
            error=RuntimeError(summary),
        ).to_dict()
    if changes is not None:
        event["changed"] = changes["changed_count"] > 0
        event["changes"] = changes
    return ModeOutcome(event)


def run_task_mode(
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: Any,
    *,
    task_kind: str = "",
    config_result: Any = None,
    run_project: Any = None,
    run_research: Any = None,
    run_planning: Any = None,
) -> ModeOutcome:
    kind = _task_kind(task_kind or getattr(frame, "task_kind", ""))
    if kind == "hybrid":
        outcome = run_entry_kernel(frame, work, hooks, deps, task_kind="hybrid", config_result=config_result)
        session = getattr(frame, "entry_session", None)
        tools = getattr(frame, "entry_research_tools", None)
        enriched = _enrich_hybrid_outcome(frame, work, outcome, session=session, research_tools=tools)
        changes = getattr(tools, "changes", None)
        has_changes = getattr(changes, "has_changes", None)
        recorder = getattr(getattr(deps, "state", None), "record_research_changes", None)
        if callable(has_changes) and has_changes() and callable(recorder):
            recorder(getattr(frame, "run_id", ""), changes, getattr(frame.request, "session_id", ""))
        return enriched
    if kind == "project" and callable(run_project):
        return cast(ModeOutcome, run_project(frame, work, hooks, config_result=config_result))
    if kind == "research" and callable(run_research):
        return cast(ModeOutcome, run_research(frame, hooks))
    if kind == "planning" and callable(run_planning):
        return cast(ModeOutcome, run_planning(frame, work, config_result=config_result))
    return run_entry_kernel(frame, work, hooks, deps, task_kind=kind, config_result=config_result)


def _enrich_hybrid_outcome(
    frame: RunFrame, work: RunWork, outcome: ModeOutcome, *, session: Any = None, research_tools: Any = None,
) -> ModeOutcome:
    event = dict(outcome.event or {})
    if research_tools is not None:
        from codey.operations.research_flow import research_payload_from_tools

        request = getattr(frame, "request", None)
        event["research"] = research_payload_from_tools(
            research_tools,
            question=str(getattr(request, "task", "") or ""),
            summary=str(event.get("summary", "") or ""),
            stop_reason=str(event.get("stop_reason", "") or ""),
            turns=int(event.get("turns", 0) or 0),
            synthesis_id=str(getattr(research_tools, "synthesis_id", "") or ""),
        )
    try:
        evidence = getattr(work, "evidence", None)
        receipt = dict(event.get("receipt") or {})
        display = dict(receipt.get("display") or {})
        # 1) Evidence archiving: bounded local facts for review/display.
        try:
            rendered = evidence.render_for_review() if evidence is not None else ""
        except Exception:
            rendered = ""
        if rendered and "evidence" not in display:
            display["evidence"] = str(rendered)[:2000]
        # Strict research quality is decided in completion_gate before done;
        # display never re-judges quality after the kernel verdict.
        # 2) Changed files projection: already-decided session facts only.
        try:
            edited = dict(getattr(session, "edited_files", {}) or {}) if session is not None else dict[str, object]()
            verifs = list(getattr(session, "verifications", ()) or []) if session is not None else list[dict[str, Any]]()
            if edited:
                display["changed_files"] = sorted(str(k)[:240] for k in edited)[:20]
                try:
                    last = verifs[-1] if verifs else dict[str, object]()
                    display["verification"] = str(last.get("command", "") or "")[:240] if isinstance(last, dict) else ""
                except Exception:
                    pass
        except Exception:
            pass
        # 3) SSE/final display: keep single-session result, add projections.
        # SSE turn/tool events were already emitted via hooks.on_event in the
        # kernel; here we keep the ModeOutcome display shape compatible.
        if display != (event.get("receipt") or {}).get("display", {}):
            receipt["display"] = display
            event["receipt"] = receipt
    except Exception:
        pass
    from dataclasses import replace

    return replace(outcome, event=event)


def run_task_submission(deps: Any, request: TaskSubmission) -> None:
    # Lazy imports: task_run imports task_phases.dispatch which imports this
    # module for run_task_mode; top-level import would cycle.
    from codey.operations.task_run import (
        execute_task_run,
        prepare_submission,
        release_unstarted_submission,
    )
    from codey.runtime.write.task_runtime import TaskRuntime

    def prepare(request: TaskSubmission) -> TaskSubmission | None:
        prepared = prepare_submission(deps.state, request)
        if prepared is None:
            return None
        try:
            prepared = _admit_api_selection(deps.state, prepared)
            from codey.operations.task_phases.lifecycle import _ensure_durable_task

            if _ensure_durable_task(deps.state, prepared):
                checkpoint = deps.state.durable.task(prepared.run_id)
                deps.state.durable.start_task(prepared.run_id, state=dict(checkpoint.state))
            return prepared
        except Exception:
            release_unstarted_submission(deps.state, prepared)
            raise

    def execute(request: TaskSubmission) -> Any:
        if not request.model_selection:
            return execute_task_run(deps, request)
        from codey.runtime.core.api_selection import ApiRunSelection

        selection = ApiRunSelection.from_payload(request.model_selection)

        def connect_selected(provider_id: str) -> Any:
            if provider_id != selection.connection_id:
                raise ValueError("admitted API connection cannot switch during this run")
            return (deps.connect_provider or deps.state.get_provider)(provider_id)

        return execute_task_run(replace(deps, connect_provider=connect_selected), request)

    runtime = TaskRuntime(
        execute,
        prepare=prepare,
        on_unstarted_failure=partial(release_unstarted_submission, deps.state),
        durable=deps.state.durable,
    )
    runtime.run(request)


def _admit_api_selection(state: Any, request: TaskSubmission) -> TaskSubmission:
    from codey.providers.api_connections import capture_selection, connection_for
    from codey.providers.catalog import API_CONNECTIONS
    from codey.runtime.core.api_selection import ApiRunSelection
    durable_payload: dict[str, object] = {}
    for candidate_id in (request.previous_run_id, request.run_id):
        if not candidate_id:
            continue
        try:
            checkpoint = state.durable.task(candidate_id)
        except (KeyError, AttributeError):
            continue
        snapshot = checkpoint.provider_snapshot
        candidate = snapshot.get("model_selection") if isinstance(snapshot, Mapping) else None
        if isinstance(candidate, Mapping):
            durable_payload = dict(candidate)
            break
    payload = durable_payload or request.model_selection
    if payload:
        selection = ApiRunSelection.from_payload(payload)
        if request.provider_id != selection.connection_id:
            raise ValueError("recovery must use the admitted API connection")
        # Validate availability and credential scope before starting any request.
        connection_for(selection.connection_id).validate_selection(selection)
    elif request.provider_id in API_CONNECTIONS:
        if request.previous_run_id and not durable_payload:
            raise ValueError("original API selection is unavailable; cannot guess a recovery model")
        selection = capture_selection(request.provider_id)
    else:
        return request
    state.run_registry.bind_api_selection(request.run_id, selection)
    return replace(request, model_selection=selection.to_payload())


__all__ = [
    "build_task_policy_for_entry",
    "start_task_session",
    "run_entry_kernel",
    "run_task_mode",
    "run_task_submission",
]
