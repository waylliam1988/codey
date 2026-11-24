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
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from codey.agents.handoff import ConversationContext
from codey.agents.tools import AgentToolFns
from codey.knowledge.changes import KnowledgeChanges
from codey.memory.context import MemoryContext, retrieve_context
from codey.operations.kernel_contracts import KernelResult
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_state import TaskState
from codey.providers.base import ChatProvider
from codey.providers.compat_transport import provider_window_ref, require_callable
from codey.providers.transport import ProviderTransport
from codey.research.tools import ResearchTools
from codey.runtime.core.outcome import OperationOutcome
from codey.utils.object_fields import object_fields, sequence_items
from codey.utils.refs import coerce_int
from codey.workspace.changes import ChangeTracker
from codey.workspace.config import ProjectConfigLoadResult

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.operations.task_run import TaskRunDeps
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.conversation_window import WindowActivationError, activate_window
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.result import ModeOutcome
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
)
from codey.policies.capabilities import BROWSER_CAPABILITIES
from codey.task.kind import ui_mode
from codey.task.model import HandoffRun, ModelHandoffRejected, TaskSubmission


class ProjectMode(Protocol):
    def __call__(self, frame: RunFrame, work: RunWork, hooks: RunHooks, /, *,
                 config_result: ProjectConfigLoadResult | None = None) -> ModeOutcome: ...


class PlanningMode(Protocol):
    def __call__(self, frame: RunFrame, work: RunWork, /, *,
                 config_result: ProjectConfigLoadResult | None = None) -> ModeOutcome: ...


@runtime_checkable
class _ResearchChangeRecorder(Protocol):
    def record_research_changes(self, run_id: str, changes: object, session_id: str = "") -> None: ...


_TASK_KINDS = frozenset({"project", "research", "hybrid", "planning", "planning_readonly", "readonly"})


@dataclass(frozen=True)
class FreshSessionOutcome:
    ok: bool
    error: str = ""


def _open_fresh_session(
    conversation: ConversationContext, provider: ChatProvider | ProviderTransport, *, provider_id: str, kind: str, project: str = "",
) -> FreshSessionOutcome:
    """Open a fresh provider session; reset the window only on success.

    重置失败一律返回失败（调用方停止并报 provider failure），绝不吞掉
    异常后继续向旧会话发送；非成功路径绝不触碰原窗口与累计预算。
    """
    try:
        require_callable(provider, "new_chat")()
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


def build_task_policy_for_entry(request: TaskSubmission, task_kind: object) -> TaskPolicy:
    from codey.agents.protocol import task_forbids_verification
    from codey.policies.task_policy import policy_for_dispatch

    kind = _task_kind(task_kind)
    strict = kind == "research" or request.strict_research is True
    policy = policy_for_dispatch(request, kind, strict_research=strict)
    if policy is None:
        raise RuntimeError("task policy configuration failed")
    if task_forbids_verification(request.task):
        policy = replace(policy, grants=policy.grants - {"project.verify"},
                         denied_capabilities=policy.denied_capabilities | {"project.verify"},
                         source=policy.source + ";verification:forbidden")
    if request.project and policy.allows("project.write"):
        from codey.operations.project_delivery import declared_edit_scope, declared_project_checks
        from codey.policies.task_policy import TASK_POLICY_FILE_SCOPE_VERSION

        checks = declared_project_checks(request.task)
        if checks:
            policy = replace(policy, required_checks=tuple(dict.fromkeys((*policy.required_checks, *checks))))
        if edit_scope := declared_edit_scope(Path(request.project), request.task):
            policy = replace(policy, edit_scope=edit_scope, version=TASK_POLICY_FILE_SCOPE_VERSION)
    return policy


def start_task_session(frame: RunFrame, work: RunWork, policy: TaskPolicy, kind: str,
                       *, durable: DurableSession | None = None) -> TaskSession:
    from codey.operations.task_session import TaskSession
    from codey.task.web_intent import explicit_web_intent, restored_source_requirements

    existing = frame.entry_session
    if existing is not None:
        if existing.policy != policy:
            raise ValueError("task continuation cannot replace authorization")
        return existing
    request = frame.request
    required_urls = (restored_source_requirements(durable.task(frame.run_id).state) if durable is not None
                     else explicit_web_intent(request.task).urls)
    session = TaskSession(
        policy=policy,
        task_kind=_task_kind(kind),
        project=frame.project_text,
        max_turns=max(1, request.max_turns or 8),
        task_text=request.task,
        required_source_urls=required_urls,
        handoff=frame.handoff,
        handoff_source_urls=frame.handoff_source_urls,
        project_changes_required=request.project_changes_required is True,
        verification_forbidden=("project.verify" in policy.denied_capabilities),
    )
    session.set_workspace_state(work.evidence.workspace_revision, work.evidence.workspace_fingerprint)
    # The entry owns the common fact view: stash it on the frame for strategy
    # phases so project/research/planning projections read the same session
    # instead of forking their own facts.
    frame.entry_session = session
    return session


def _entry_policy_with_recovery(frame: RunFrame, durable: DurableSession | None, kind: str) -> TaskPolicy:
    request = frame.request
    from codey.operations.recovery import durable_task_for, policy_from_durable_task

    durable = durable_task_for(durable, frame.run_id)
    incoming = frame.entry_policy
    if incoming is None:
        incoming = build_task_policy_for_entry(replace(request, project=frame.project_text or None), kind)
    if request.previous_run_id:
        if frame.entry_policy is None:
            raise RuntimeError("original task authorization unavailable for approval continuation")
        return incoming
    recovered = durable is not None and bool(durable.task(frame.run_id).state.get("task_policy"))
    if not recovered:
        recovered_outcomes: object = getattr(frame, "recovered_tool_outcomes", ())
        settled_outcomes: object = getattr(frame, "settled_tool_outcomes", ())
        if recovered_outcomes or settled_outcomes:
            raise RuntimeError("durable task policy recovery requires the original authorization")
        return incoming
    # A resumed run must use the policy recorded at its original entry. Never
    # replace a missing or malformed persisted policy with the new request's
    # capabilities; missing grants or requirements stop recovery.
    return policy_from_durable_task(durable, task_id=frame.run_id)


def _entry_check_conflict(policy: TaskPolicy, request: TaskSubmission, kind: str) -> None:
    requires = (request.project_changes_required is True
                or "project_changes_required" in policy.required_checks)
    if requires and not policy.allows("project.write"):
        raise RuntimeError(
            "project_changes_required without project.write: task declares must-change "
            "but entry grants no write permission"
        )


def _entry_executors(frame: RunFrame, deps: TaskRunDeps, policy: TaskPolicy) -> tuple[Path | None, AgentToolFns | None, ResearchTools | None]:
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
        from codey.agents.tools import build_project_tool_fns

        managed_outputs = deps.managed_outputs
        tool_fns = build_project_tool_fns(
            state_home=managed_outputs.root.parent if managed_outputs is not None else None,
        )
    research_tools = None
    browser_actions = any(policy.allows(grant) for grant in BROWSER_CAPABILITIES)
    if browser_actions or any(policy.allows(grant) for grant in ("web.read", "knowledge.read", "knowledge.write", "knowledge.link")):
        from codey.operations.task_execution import build_research_tools

        research_tools = build_research_tools(
            deps, session_id=request.session_id, project=frame.project_text,
            browser_sources=policy.strict_research, browser_actions=browser_actions)
        if policy.allows("web.read"):
            assert frame.entry_session is not None
            research_tools.direct_urls = frame.entry_session.required_source_urls
    return project_path, tool_fns, research_tools


def _entry_provider_sink(frame: RunFrame, deps: TaskRunDeps, kind: str) -> tuple[object, KernelEffectSink | None, Event]:
    request = frame.request
    intent_sink = None
    active_provider: object = frame.provider
    mutations = deps.runtime_mutations
    if mutations is not None and request.session_id and frame.run_id:
        from codey.operations.task_effects import KernelEffectSink

        mutations.mark_writer_running(request.session_id, frame.run_id,
                                      provider_id=frame.provider_id)
        from codey.operations.task_effects import admitted_durable

        intent_sink = KernelEffectSink(
            mutations, session_id=request.session_id, run_id=frame.run_id,
            provider_id=frame.provider_id,
            phase="research" if kind == "research" else "writer",
            managed_outputs=deps.managed_outputs,
            durable=admitted_durable(deps.state, frame.run_id),
        )
    if frame.conversation is not None:
        from codey.operations.provider_session import ConversationProvider

        active_provider = ConversationProvider(active_provider, frame.conversation)
    stop_flag = deps.state.stop_flag_for(frame.run_id)
    return active_provider, intent_sink, stop_flag


def _entry_project_tracker(project_path: Path | None, deps: TaskRunDeps, policy: TaskPolicy, run_id: str) -> ChangeTracker | None:
    if project_path is None or not policy.allows("project.write"):
        return None
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        from codey.operations.task_changes import task_change_tracker

        tracker: ChangeTracker | None = task_change_tracker(deps.state, project_path, run_id)
        if tracker is None:
            raise ValueError("project change tracker is missing")
        return tracker
    except Exception as exc:
        raise RecoveryFailed(f"project change tracking unavailable: {exc}") from exc


def _entry_project_receipt(frame: RunFrame, work: RunWork, hooks: RunHooks, deps: TaskRunDeps,
                           session: TaskSession, result: KernelResult, tracker: ChangeTracker | None) -> tuple[dict[str, object], dict[str, object] | None]:
    from codey.operations.task_session import session_checks_passed
    from codey.runs.receipt import build_task_receipt
    from codey.runs.task_diff import record_task_changes

    if tracker is None or not session.edited_files:
        return {"display": {"summary": str(result.summary or "")[:2000]}}, None
    changes = deps.collect_changes(frame.project_text, tracker)
    checks_passed = session_checks_passed(session, result.proof)
    if changes.get("ok") is not True:
        changes = {**changes, "files": [{"path": path} for path in sorted(session.edited_files)],
                   "changed_count": len(session.edited_files), "mode": ""}
    receipt: dict[str, object] = build_task_receipt(changes, proof=result.proof, checks_passed=checks_passed).to_dict()
    if work.ledger is not None:
        hooks.append_ledger(lambda ledger: record_task_changes(
            ledger, deps.managed_outputs, changes, checks_passed=checks_passed, receipt=receipt))
    value = changes.get("files")
    files = list(value[:3]) if sequence_items(value) else []
    payload: dict[str, object] = {"changed_count": changes.get("changed_count", 0),
               "files": files, "mode": changes.get("mode"),
               "project": frame.project_text}
    return receipt, payload


def _entry_context(frame: RunFrame, deps: TaskRunDeps, policy: TaskPolicy) -> tuple[str, MemoryContext]:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import _project_context

    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    memory = retrieve_context(deps.state.memory, task=request.task, project=request.project or "",
                              session_id=request.session_id, run_id=frame.run_id)
    parts = [memory.text] if memory.text else []
    if policy.allows("project.read"):
        workspace = _project_context(AgentRequest(
            provider=frame.provider, project=Path(frame.project_text), task=request.task,
            permission_profile="coding_writer" if policy.allows("project.write") else "planning_readonly",
            trace_recorder=frame.trace,
        ))
        if workspace:
            parts.append(workspace)
    return "\n\n".join(parts), memory


def _workspace_writer(frame: RunFrame, state: TaskState, stack: ExitStack) -> Callable[[], None]:
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


def run_entry_kernel(frame: RunFrame, work: RunWork, hooks: RunHooks, deps: TaskRunDeps,
                     *, task_kind: str = "", config_result: ProjectConfigLoadResult | None = None, continuation_followup: str = "") -> ModeOutcome:
    from codey.operations.task_execution import close_research_tools

    try:
        with ExitStack() as writer_stack:
            return _run_entry_kernel(frame, work, hooks, deps,
                                     task_kind=task_kind, config_result=config_result,
                                     continuation_followup=continuation_followup, writer_stack=writer_stack)
    finally:
        close_research_tools(frame.entry_research_tools)


def _run_entry_kernel(  # noqa: PLR0915
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: TaskRunDeps,
    *,
    task_kind: str = "",
    config_result: ProjectConfigLoadResult | None = None,
    continuation_followup: str = "",
    writer_stack: ExitStack,
) -> ModeOutcome:
    from codey.operations.task_loop import run_task_kernel
    from codey.workspace.context_admission import request_scope
    request = frame.request
    kind = _task_kind(task_kind or frame.task_kind)
    if not frame.project_text and kind == "chat":
        from codey.workspace.session import session_workspace

        state_home = deps.workspace_revisions.state_home
        frame.project_text = str(session_workspace(state_home, request.session_id))
    policy = _entry_policy_with_recovery(frame, deps.state.durable, kind)
    _entry_check_conflict(policy, request, kind)
    session = start_task_session(frame, work, policy, kind, durable=deps.state.durable)
    from codey.operations.recovery import durable_task_for, record_entry_policy

    record_entry_policy(
        deps.runtime_mutations,
        session_id=request.session_id, run_id=frame.run_id, policy=policy,
        durable=durable_task_for(deps.state.durable, frame.run_id),
    )
    project_path, tool_fns, research_tools = _entry_executors(frame, deps, policy)
    if project_path is not None and policy.allows("project.verify"):
        from codey.operations.task_context import safe_verification_candidates
        from codey.workspace.config import load_project_config

        config_result = config_result if config_result is not None else load_project_config(project_path)
        config = config_result.config
        session.verification_candidate_loader = partial(safe_verification_candidates, project_path,
            configured_commands=config.verification_commands, ignored_paths=config.ignored_paths)
        session.verification_candidates = session.verification_candidate_loader()
    previous_tools = frame.entry_research_tools
    if research_tools is not None and previous_tools is not None:
        research_tools.ledger = previous_tools.ledger
    frame.entry_research_tools = research_tools
    # 会话生命周期：仅 fresh_chat 且 new_chat 成功后才开启新窗口（首发之前）；
    # 重置失败立即停止并报 provider failure，不向旧会话发送；继续会话时
    # 保留原窗口与累计预算。
    if bool(frame.fresh_chat):
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
    context_text, memory = _entry_context(frame, deps, policy)
    ensure_workspace_writer = _workspace_writer(frame, deps.state, writer_stack) if kind == "chat" else None
    try:
        ignored = config_result.config.ignored_paths if config_result is not None and config_result.config is not None else ()
    except Exception:
        ignored = ()
    try:
        tracker = _entry_project_tracker(project_path, deps, policy, frame.run_id)
        result = run_task_kernel(
            session,
            request=KernelRunRequestBuilder(
                    provider=active_provider,
                    run_id=frame.run_id,
                    effect_scope="task",
                    provider_id=frame.provider_id,
                    user_task=request.task,
                    stop_flag=stop_flag,
                    context_text="\n\n".join(part for part in (context_text, continuation_followup) if part),
                    context_scope=request_scope(request.project, request.session_id),
                    memory_sources=memory.sources,
                    memory_spans=memory.spans,
                    memory_traces=memory.traces,
                    collect_memory_candidates=memory.collect_candidates,
                    attempt_validate_memory_sources=lambda actual: actual == memory.sources and memory.valid(deps.state.memory),
                    window_ref=provider_window_ref(active_provider),
                    task_guidance=task_guidance_for_policy(session.policy, task_kind=session.task_kind),
                ).build(execution=KernelExecutionDeps(
                    executors={},
                    intent_sink=intent_sink,
                    project_path=project_path,
                    tool_fns=tool_fns,
                    change_tracker=tracker,
                    research_tools=research_tools,
                    permission_profile="research" if kind == "research" else "coding_writer",
                    workspace_ignored_paths=ignored,
                    workspace_revision_store=deps.workspace_revisions,
                    ensure_workspace_writer=ensure_workspace_writer,
                ), observation=KernelObservationDeps(
                    on_event=hooks.on_event,
                    on_shell_request=hooks.on_shell_request,
                    completion_context={
                        "run_id": frame.run_id,
                        "task": request.task,
                        "question": request.task,
                        "project": frame.project_text,
                        "execution_evidence": work.evidence,
                        "analysis_run_payloads": work.analysis_run_payloads,
                        "research_ledger": research_tools.ledger if research_tools is not None else None,
                    },
                    trace_recorder=frame.trace,
                    on_boundary=durable_generation_boundary_callback(
                        durable=deps.state.durable,
                        session_id=str(request.session_id or ""),
                        task_id=frame.run_id,
                        sink=intent_sink,
                    ),
                )),
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

    with suppress(Exception):
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
    if result.proof is not None:
        receipt["completion_proof"] = result.proof.to_payload()
    event: dict[str, object] = {
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

        failure_payload: dict[str, object] = capture_provider_failure(
            model=PROVIDER_LABELS.get(frame.provider_id, frame.provider_id), action="task", page=None,
            error=RuntimeError(summary),
        ).to_dict()
        event["provider_failure"] = failure_payload
    if changes is not None:
        count = changes["changed_count"]
        event["changed"] = isinstance(count, int) and count > 0
        event["changes"] = changes
    return ModeOutcome(event)


def run_task_mode(
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: TaskRunDeps,
    *,
    task_kind: str = "",
    config_result: ProjectConfigLoadResult | None = None,
    run_project: ProjectMode | None = None,
    run_research: Callable[[RunFrame, RunHooks], ModeOutcome] | None = None,
    run_planning: PlanningMode | None = None,
) -> ModeOutcome:
    kind = _task_kind(task_kind or frame.task_kind)
    if kind == "hybrid":
        outcome = run_entry_kernel(frame, work, hooks, deps, task_kind="hybrid", config_result=config_result)
        session = frame.entry_session
        tools = frame.entry_research_tools
        enriched = _enrich_hybrid_outcome(frame, work, outcome, session=session, research_tools=tools)
        changes = tools.changes if tools is not None else None
        if isinstance(changes, KnowledgeChanges) and changes.has_changes() and isinstance(deps.state, _ResearchChangeRecorder):
            deps.state.record_research_changes(frame.run_id, changes, frame.request.session_id)
        return enriched
    if kind == "project" and run_project is not None:
        return run_project(frame, work, hooks, config_result=config_result)
    if kind == "research" and run_research is not None:
        return run_research(frame, hooks)
    if kind == "planning" and run_planning is not None:
        return run_planning(frame, work, config_result=config_result)
    return run_entry_kernel(frame, work, hooks, deps, task_kind=kind, config_result=config_result)


def _enrich_hybrid_outcome(
    frame: RunFrame, work: RunWork, outcome: ModeOutcome, *, session: TaskSession | None = None, research_tools: ResearchTools | None = None,
) -> ModeOutcome:
    event = dict(outcome.event or {})
    if research_tools is not None:
        from codey.operations.research_flow import research_payload_from_tools

        request = frame.request
        event["research"] = research_payload_from_tools(
            research_tools,
            question=request.task,
            summary=str(event.get("summary", "") or ""),
            stop_reason=str(event.get("stop_reason", "") or ""),
            turns=coerce_int(event.get("turns", 0)),
            synthesis_id=research_tools.synthesis_id,
        )
    raw_receipt = event.get("receipt")
    receipt = dict(raw_receipt) if object_fields(raw_receipt) else dict[str, object]()
    raw_display = receipt.get("display")
    display = dict(raw_display) if object_fields(raw_display) else dict[str, object]()
    rendered = work.evidence.render_for_review()
    if rendered and "evidence" not in display:
        display["evidence"] = rendered[:2000]
    if session is not None and session.edited_files:
        display["changed_files"] = sorted(session.edited_files)[:20]
        display["verification"] = session.verifications[-1]["command"][:240] if session.verifications else ""
    if display != raw_display:
        receipt["display"] = display
        event["receipt"] = receipt
    from dataclasses import replace

    return replace(outcome, event=event)


def run_task_submission(deps: TaskRunDeps, request: TaskSubmission) -> None:
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

    def execute(request: TaskSubmission) -> OperationOutcome | None:
        if not request.model_selection:
            return execute_task_run(deps, request)
        from codey.runtime.core.api_selection import ApiRunSelection

        selection = ApiRunSelection.from_payload(request.model_selection)

        def connect_selected(provider_id: str) -> ChatProvider:
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


def _admit_api_selection(state: TaskState, request: TaskSubmission) -> TaskSubmission:
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
    if isinstance(request.input, HandoffRun) and not payload:
        raise ModelHandoffRejected("Handoff requires an explicit API model selection.")
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
        if isinstance(request.input, HandoffRun):
            raise ModelHandoffRejected("Handoff requires an explicit API model selection.")
        return request
    if isinstance(request.input, HandoffRun):
        from codey.operations.model_handoff import OBSERVATION_DENIALS, authorize_model_handoff

        if request.continue_task or request.previous_run_id or request.input.observe_only and request.project_changes_required:
            raise ModelHandoffRejected("Handoff must begin a new run with compatible task requirements.")
        authorize_model_handoff(state.durable, request=request.input, target=selection, session_id=request.session_id,
                                run_id=request.run_id, project=request.project)
        if request.input.observe_only:
            request = replace(request, denied_capabilities=tuple(sorted(set(request.denied_capabilities) | OBSERVATION_DENIALS)))
    state.run_registry.bind_api_selection(request.run_id, selection)
    selection_payload: dict[str, object] = selection.to_payload()
    return replace(request, model_selection=selection_payload)


__all__ = [
    "build_task_policy_for_entry",
    "start_task_session",
    "run_entry_kernel",
    "run_task_mode",
    "run_task_submission",
]
