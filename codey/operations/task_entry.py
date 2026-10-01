"""Single task entry: runtime submission + mode dispatch (one import point).

Runtime entry ``run_task_submission`` and mode entry ``run_task_mode`` converge
here. Hybrid runs on the shared entry kernel with one ``TaskSession``/common
fact view; project/research/planning delegate to their dedicated flows with
their own sessions. All production tool calls converge in
``run_task_kernel``/``execute_turn``; ``done`` converges in the single
``completion_gate``.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.result import ModeOutcome
from codey.task.model import TaskSubmission, execution_task

_TASK_KINDS = frozenset({"project", "research", "hybrid", "planning", "planning_readonly", "readonly"})


@dataclass(frozen=True)
class FreshSessionOutcome:
    ok: bool
    error: str = ""


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
    return "project" if kind in {"auto"} else kind


def build_task_policy_for_entry(request: Any, task_kind: object) -> Any:
    from codey.policies.task_policy import policy_for_dispatch

    kind = _task_kind(task_kind)
    strict = kind == "research" or bool(getattr(request, "strict_research", False) is True)
    return policy_for_dispatch(request, kind, strict_research=strict)


def _build_research_tools(deps: Any, *, session_id: str, project: str) -> Any | None:
    from codey.operations.task_execution import build_research_tools

    return build_research_tools(deps, session_id=session_id, project=project)


def _entry_verification_forbidden(request: Any) -> bool:
    from codey.agents.protocol import task_forbids_verification

    try:
        return bool(task_forbids_verification(str(getattr(request, "task", "") or "")))
    except Exception:
        return False


def _create_entry_session(frame: RunFrame, work: RunWork, policy: Any, kind: str) -> Any:
    from codey.operations.task_session import TaskSession

    request = frame.request
    session = TaskSession(
        policy=policy,
        task_kind=kind,
        project=frame.project_text,
        max_turns=max(1, int(getattr(request, "max_turns", 8) or 8)),
        task_text=execution_task(request),
        handoff=str(getattr(frame, "handoff", "") or ""),
        project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
        coding_context_enabled=bool(getattr(request, "coding_context_enabled", True) is True),
        verification_forbidden=_entry_verification_forbidden(request),
    )
    try:
        ev = getattr(work, "evidence", None)
        if ev is not None:
            session.set_workspace_state(
                getattr(ev, "workspace_revision", 0),
                getattr(ev, "workspace_fingerprint", ""),
            )
    except Exception:
        pass
    # The entry owns the common fact view: stash it on the frame for strategy
    # phases so project/research/planning projections read the same session
    # instead of forking their own facts.
    with contextlib.suppress(Exception):
        frame.entry_session = session
    return session


def _entry_policy_with_recovery(frame: RunFrame, deps: Any, kind: str) -> Any:
    request = frame.request
    from codey.operations.recovery import rebuilt_policy_from_log

    mutations = getattr(deps, "runtime_mutations", None)
    session_log = getattr(mutations, "session_log", None) if mutations is not None else None
    incoming = getattr(frame, "entry_policy", None)
    if incoming is None:
        incoming = build_task_policy_for_entry(request, kind)
    if getattr(request, "previous_run_id", ""):
        if getattr(frame, "entry_policy", None) is None:
            raise RuntimeError("original task authorization unavailable for approval continuation")
        return incoming
    recovered = bool(getattr(frame, "recovered_tool_outcomes", ()) or ())
    if not recovered:
        return incoming
    if session_log is None:
        raise RuntimeError("recovery policy log unavailable")
    # A resumed run must use the policy recorded at its original entry. Never
    # replace a missing or malformed persisted policy with the new request's
    # capabilities; recovery.py returns a control-only policy in that case.
    return rebuilt_policy_from_log(
        session_log,
        incoming,
        session_id=request.session_id,
        run_id=frame.run_id,
    )


def _entry_check_conflict(policy: Any, request: Any, kind: str) -> None:
    try:
        requires = bool(getattr(request, "project_changes_required", False) is True)
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


def _entry_apply_auto(frame: RunFrame, policy: Any, kind: str) -> tuple[Any, str]:
    from codey.policies.task_policy import apply_auto_plan

    request = frame.request
    try:
        requires = bool(getattr(request, "project_changes_required", False) is True)
    except Exception:
        requires = False
    auto_plan = ""
    if str(getattr(request, "intent", "") or "").strip().lower() != "auto":
        return policy, ""
    auto_plan = _decide_auto(frame)
    if auto_plan == "__direct__":
        return policy, "__direct__"
    if auto_plan:
        policy = apply_auto_plan(policy, auto_plan)
        if requires and kind in {"project", "hybrid"}:
            try:
                allows = bool(policy.allows("project.write"))
            except Exception:
                allows = False
            if not allows:
                raise RuntimeError(
                    "project_changes_required without project.write: auto plan narrowed away write"
                )
    return policy, auto_plan


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
        try:
            from codey.agents.tools import DEFAULT_TOOL_FNS
        except Exception:
            DEFAULT_TOOL_FNS = None  # type: ignore[assignment]
        tool_fns = DEFAULT_TOOL_FNS
    research_tools = None
    if any(policy.allows(grant) for grant in ("web.read", "knowledge.read", "knowledge.write", "knowledge.link")):
        research_tools = _build_research_tools(
            deps, session_id=request.session_id, project=frame.project_text)
    return project_path, tool_fns, research_tools


def _validate_recovered_rows(raw_rows: list[Any]) -> None:
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_recovery_result import _strict_slot_index

    seen: set[tuple[int, int]] = set()
    for row in raw_rows:
        try:
            if getattr(row, "call", None) is None or getattr(row, "outcome", None) is None:
                raise RecoveryFailed("missing call/outcome")
            turn = _strict_slot_index(getattr(row, "turn", None), field="turn")
            index = _strict_slot_index(getattr(row, "tool_index", None), field="tool_index")
            if (turn, index) in seen:
                raise RecoveryFailed(f"duplicate recovered slot: turn={turn} index={index}")
            seen.add((turn, index))
        except RecoveryFailed:
            raise
        except Exception as exc:
            raise RecoveryFailed(f"malformed recovered row: {exc}") from exc


def _sort_recovered_rows(raw_rows: list[Any]) -> list[Any]:
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        return sorted(
            raw_rows,
            key=lambda r: (int(getattr(r, "turn", 0) or 0), int(getattr(r, "tool_index", 0) or 0)),
        )
    except Exception as exc:
        raise RecoveryFailed(f"recovered row ordering failed: {exc}") from exc


def _replay_recovered_facts(session: Any, recovered_rows: list[Any], recovered_results: list[Any]) -> None:
    """Replay facts from the single-built recovered results (no rebuild)."""
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        from codey.operations.kernel_facts import record_facts_for_result
        from codey.operations.kernel_recovery_result import frame_outcome_exit_code, frame_outcome_ok
    except Exception as exc:
        raise RecoveryFailed(f"recovery helpers unavailable: {exc}") from exc
    for row, prior in zip(recovered_rows, recovered_results, strict=True):
        try:
            record_facts_for_result(session, row.call, prior, ok=frame_outcome_ok(row),
                                    exit_code=frame_outcome_exit_code(row))
        except Exception as exc:
            raise RecoveryFailed(f"recovered facts replay failed: {exc}") from exc


def _entry_recovery(frame: RunFrame, session: Any) -> tuple[dict[str, Any], list[Any], int, list[Any]]:
    """Rebuild recovery state; any failure raises RecoveryFailed (fail-closed).

    Single construction: ``delivered_from_frame`` builds each row once via
    the unified builder; facts and ``initial_results`` reuse those exact
    objects so facts/initial can never drift into two versions.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        from codey.operations.recovery import delivered_from_frame
        from codey.operations.task_session import turn_effect_id
    except Exception as exc:
        raise RecoveryFailed(f"recovery import failed: {exc}") from exc
    if not callable(delivered_from_frame):
        raise RecoveryFailed("recovery builder unavailable")
    raw_rows = list(getattr(frame, "recovered_tool_outcomes", ()) or ())
    _validate_recovered_rows(raw_rows)
    recovered_rows = _sort_recovered_rows(raw_rows)
    try:
        delivered = delivered_from_frame(frame, effect_scope="task")
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovery delivery failed: {exc}") from exc
    # Reuse the single-built objects in sorted order for facts + initial.
    try:
        identities = [
            turn_effect_id(f"{frame.run_id}:task", int(r.turn), int(r.tool_index))
            for r in recovered_rows
        ]
        recovered_results = [delivered[ident] for ident in identities]
    except Exception as exc:
        raise RecoveryFailed(f"recovered delivery map incomplete: {exc}") from exc
    _replay_recovered_facts(session, recovered_rows, recovered_results)
    try:
        resume_start = max(int(getattr(r, "turn", 0) or 0) for r in recovered_rows) + 1 if recovered_rows else 1
        resume_start = max(1, resume_start)
    except Exception as exc:
        raise RecoveryFailed(f"recovered resume turn unreadable: {exc}") from exc
    return delivered, recovered_rows, resume_start, list(recovered_results)


def _entry_provider_sink(frame: RunFrame, deps: Any, kind: str) -> tuple[Any, Any, Any]:
    request = frame.request
    intent_sink = None
    active_provider = frame.provider
    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is not None and request.session_id and frame.run_id:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        mutations.mark_writer_running(request.session_id, frame.run_id,
                                      provider_id=frame.provider_id)
        intent_sink = KernelEffectSink(
            mutations, session_id=request.session_id, run_id=frame.run_id,
            provider_id=frame.provider_id,
            phase="research" if kind == "research" else "writer",
            recovered_batch_id=str(getattr(frame, "recovered_tool_result_batch_id", "") or ""),
            managed_outputs=getattr(deps, "managed_outputs", None),
        )
        active_provider = KernelRecordedProvider(active_provider, intent_sink)
    if getattr(frame, "conversation", None) is not None:
        from codey.operations.provider_session import ConversationProvider

        active_provider = ConversationProvider(active_provider, frame.conversation)
    stop_flag = getattr(getattr(deps, "state", None), "run_registry", None)
    stop_flag = getattr(stop_flag, "stop_flag", None)
    return active_provider, intent_sink, stop_flag


def run_entry_kernel(frame: RunFrame, work: RunWork, hooks: RunHooks, deps: Any,
                     *, task_kind: str = "", config_result: Any = None) -> ModeOutcome:
    from codey.operations.task_execution import close_research_tools

    try:
        return _run_entry_kernel(frame, work, hooks, deps,
                                 task_kind=task_kind, config_result=config_result)
    finally:
        close_research_tools(getattr(frame, "entry_research_tools", None))


def _run_entry_kernel(
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: Any,
    *,
    task_kind: str = "",
    config_result: Any = None,
) -> ModeOutcome:
    from codey.operations.task_loop import run_task_kernel

    request = frame.request
    kind = _task_kind(task_kind or frame.task_kind)
    policy = _entry_policy_with_recovery(frame, deps, kind)
    if policy is None:
        raise RuntimeError("task policy unavailable during recovery")
    _entry_check_conflict(policy, request, kind)
    policy, auto_plan = _entry_apply_auto(frame, policy, kind)
    if auto_plan == "__direct__":
        direct = _direct_answer_outcome(frame, kind)
        if direct.event.get("stop_reason") == "done":
            return direct
        # Direct answer failed the common gate: hand the missing
        # requirements to the shared kernel instead of claiming done.
        pass
    session = _create_entry_session(frame, work, policy, kind)
    from codey.operations.recovery import record_entry_policy

    record_entry_policy(
        getattr(deps, "runtime_mutations", None),
        session_id=request.session_id, run_id=frame.run_id, policy=policy,
    )
    project_path, tool_fns, research_tools = _entry_executors(frame, deps, policy)
    frame.entry_research_tools = research_tools
    try:
        delivered, _, resume_start, initial_results = _entry_recovery(frame, session)
    except Exception as exc:
        from codey.operations.kernel_errors import RecoveryFailed as _RecoveryFailed

        reason = f"recovery failed: {exc}" if isinstance(exc, _RecoveryFailed) else f"recovery failed: {exc}"
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
        fresh = _open_fresh_session(
            frame.conversation, frame.provider,
            provider_id=frame.provider_id, kind=kind, project=frame.project_text,
        )
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
    try:
        ignored = tuple(getattr(getattr(config_result, "config", None), "ignored_paths", ()) or ())
    except Exception:
        ignored = ()
    try:
        result = run_task_kernel(
            session,
            provider=active_provider,
            executors={},
            run_id=frame.run_id,
            effect_scope="task",
            provider_id=frame.provider_id,
            project_path=project_path,
            tool_fns=tool_fns,
            research_tools=research_tools,
            managed_outputs=getattr(deps, "managed_outputs", None),
            session_id=request.session_id,
            permission_profile="research" if kind == "research" else "coding_writer",
            user_task=execution_task(request),
            stop_flag=stop_flag,
            delivered=delivered or None,
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
            start_turn=resume_start,
            initial_results=initial_results or None,
            provider_session_changed=bool(getattr(frame, "provider_session_changed", False)),
            workspace_ignored_paths=ignored,
            workspace_revision_store=getattr(deps, "workspace_revisions", None),
            trace_recorder=getattr(frame, "trace", None),
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

        summary_text = str(result.summary or "")
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
    summary = str(result.summary or "")
    receipt = {"display": {"summary": summary[:2000]}}
    proof = getattr(result, "proof", None)
    if proof is not None:
        to_payload = getattr(proof, "to_payload", None)
        if callable(to_payload):
            with _contextlib.suppress(Exception):
                receipt["completion_proof"] = to_payload()
    return ModeOutcome({
        "type": "task_done",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "summary": summary,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "max_turns": request.max_turns,
        "provider": frame.provider_id,
        "mode": kind,
        "receipt": receipt,
    })


def _decide_auto(frame: RunFrame) -> str:
    try:
        from codey.operations.auto_loop import build_auto_first_prompt, parse_auto_first_output
    except Exception:
        return ""
    request = frame.request
    provider = frame.provider
    if provider is None:
        return ""
    try:
        prompt = build_auto_first_prompt(request.task, project=frame.project_text)
        raw = provider.send(prompt)
    except Exception:
        return ""
    try:
        decision = parse_auto_first_output(raw)
    except Exception:
        return ""
    kind = str(getattr(decision, "kind", "") or "").strip().lower()
    if kind in {"chat", ""}:
        frame.task_kind = "chat"
        frame.handoff = str(getattr(decision, "answer", "") or raw or "")
        return "__direct__"
    frame.task_kind = kind if kind in _TASK_KINDS else "project"
    plan = str(getattr(decision, "plan", "") or "").strip()
    return f"ACTION: {frame.task_kind}\nPLAN: {plan}" if plan else f"ACTION: {frame.task_kind}"


def _direct_answer_gate(request: Any, answer: str, *, run_id: str = "", project: str = "") -> Any:
    """Single owner for direct-answer completion checks (no keyword guessing).

    Uses the entry's real run identity and project path when provided;
    falls back to the request only for legacy direct calls.
    """
    from codey.operations.completion_gate import evaluate as gate_evaluate
    from codey.operations.task_session import TaskSession

    effective_run_id = str(run_id or getattr(request, "run_id", "") or "")
    effective_project = str(project or getattr(request, "project", "") or "")
    policy = build_task_policy_for_entry(request, "chat")
    session = TaskSession(
        policy=policy,
        task_kind="chat",
        project=effective_project,
        max_turns=max(1, int(getattr(request, "max_turns", 8) or 8)),
        task_text=str(getattr(request, "task", "") or ""),
        project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
        verification_forbidden=_entry_verification_forbidden(request),
    )
    context = {
        "run_id": effective_run_id,
        "task": str(getattr(request, "task", "") or ""),
        "question": str(getattr(request, "task", "") or ""),
        "project": effective_project,
    }
    return gate_evaluate(session, answer, context=context)


def evaluate_direct_answer_candidate(frame: Any, answer: str) -> Any:
    """Shared direct-answer verdict for every entry (task_entry + auto_loop)."""
    request = getattr(frame, "request", None)
    return _direct_answer_gate(
        request,
        answer,
        run_id=str(getattr(frame, "run_id", "") or ""),
        project=str(getattr(frame, "project_text", "") or ""),
    )


def _direct_answer_blocked(frame: RunFrame, reason: str) -> ModeOutcome:
    request = frame.request
    return ModeOutcome({
        "type": "task_done",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "summary": reason,
        "stop_reason": "blocked",
        "turns": 1,
        "max_turns": request.max_turns,
        "provider": frame.provider_id,
        "mode": "chat",
        "receipt": {"display": {"summary": reason[:2000]}},
    })


def _direct_answer_outcome(frame: RunFrame, kind: str) -> ModeOutcome:
    request = frame.request
    summary = str(getattr(frame, "handoff", "") or "")
    try:
        verdict = evaluate_direct_answer_candidate(frame, summary)
    except Exception as exc:
        reason = (
            f"Completion gate check failed ({type(exc).__name__}: {exc}); "
            "cannot complete yet. Continue the task."
        )
        return _direct_answer_blocked(frame, reason)
    if verdict is None:
        return _direct_answer_blocked(
            frame,
            "Completion gate returned no verdict; cannot complete yet. Continue the task.",
        )
    try:
        complete = verdict.complete
    except Exception as exc:
        return _direct_answer_blocked(
            frame,
            f"Completion gate verdict unreadable ({type(exc).__name__}); "
            "cannot complete yet. Continue the task.",
        )
    if complete is True:
        return ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": summary,
            "stop_reason": "done",
            "turns": 1,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "chat",
            "receipt": {"display": {"summary": summary[:2000]}},
        })
    if complete is False:
        try:
            followup = str(getattr(verdict, "followup", "") or "")
        except Exception:
            followup = ""
        return _direct_answer_blocked(frame, followup or "Not done yet. Continue the task.")
    return _direct_answer_blocked(
        frame,
        "Completion gate verdict invalid; cannot complete yet. Continue the task.",
    )


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
        try:
            session = getattr(frame, "entry_session", None)
            tools = getattr(frame, "entry_research_tools", None)
            return _enrich_hybrid_outcome(frame, work, outcome, session=session, research_tools=tools)
        except Exception:
            return outcome
    if kind == "project" and callable(run_project):
        return run_project(frame, work, hooks, config_result=config_result)
    if kind == "research" and callable(run_research):
        return run_research(frame, hooks)
    if kind == "planning" and callable(run_planning):
        return run_planning(frame, work, config_result=config_result)
    return run_entry_kernel(frame, work, hooks, deps, task_kind=kind, config_result=config_result)


def _enrich_hybrid_outcome(
    frame: RunFrame, work: RunWork, outcome: ModeOutcome, *, session: Any = None, research_tools: Any = None,
) -> ModeOutcome:
    try:
        event = dict(outcome.event or {})
    except Exception:
        return outcome
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
            edited = dict(getattr(session, "edited_files", {}) or {}) if session is not None else {}
            verifs = list(getattr(session, "verifications", ()) or []) if session is not None else []
            if edited:
                display["changed_files"] = sorted(str(k)[:240] for k in edited)[:20]
                try:
                    last = verifs[-1] if verifs else {}
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
    try:
        return ModeOutcome(event)
    except Exception:
        return outcome


def run_task_submission(deps: Any, request: TaskSubmission) -> None:
    # Lazy imports: task_run imports task_phases.dispatch which imports this
    # module for run_task_mode; top-level import would cycle.
    from codey.operations.task_run import (
        execute_task_run,
        prepare_submission,
        release_unstarted_submission,
    )
    from codey.runtime.write.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        deps.state.runtime_log,
        lambda submission: execute_task_run(deps, submission),
        prepare=lambda submission: prepare_submission(deps.state, submission),
        on_unstarted_failure=lambda submission: release_unstarted_submission(deps.state, submission),
    )
    runtime.run(request)


__all__ = [
    "build_task_policy_for_entry",
    "run_entry_kernel",
    "run_task_mode",
    "run_task_submission",
]
