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
from pathlib import Path
from typing import Any

from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.result import ModeOutcome
from codey.task.model import TaskSubmission, execution_task

_TASK_KINDS = frozenset({"project", "research", "hybrid", "planning", "planning_readonly", "readonly"})


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
    try:
        from codey.operations.task_execution import build_research_tools
    except Exception:
        return None
    try:
        return build_research_tools(deps, session_id=session_id, project=project)
    except Exception:
        return None


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
    incoming = build_task_policy_for_entry(request, kind)
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
    research_tools = _build_research_tools(
        deps, session_id=request.session_id, project=frame.project_text)
    return project_path, tool_fns, research_tools


def _validate_recovered_rows(raw_rows: list[Any]) -> None:
    from codey.operations.kernel_recovery import RecoveryFailed

    for row in raw_rows:
        try:
            if getattr(row, "call", None) is None or getattr(row, "outcome", None) is None:
                raise ValueError("missing call/outcome")
            int(getattr(row, "turn", None))
            int(getattr(row, "tool_index", None))
        except RecoveryFailed:
            raise
        except Exception as exc:
            raise RecoveryFailed(f"malformed recovered row: {exc}") from exc


def _sort_recovered_rows(raw_rows: list[Any]) -> list[Any]:
    from codey.operations.kernel_recovery import RecoveryFailed

    try:
        return sorted(
            raw_rows,
            key=lambda r: (int(getattr(r, "turn", 0) or 0), int(getattr(r, "tool_index", 0) or 0)),
        )
    except Exception as exc:
        raise RecoveryFailed(f"recovered row ordering failed: {exc}") from exc


def _replay_recovered_facts(session: Any, recovered_rows: list[Any]) -> None:
    from codey.operations.kernel_recovery import RecoveryFailed

    try:
        from codey.operations.kernel_facts import record_facts_for_result
        from codey.operations.kernel_result import build_recovered_tool_result
    except Exception as exc:
        raise RecoveryFailed(f"recovery helpers unavailable: {exc}") from exc
    for row in recovered_rows:
        try:
            outcome_audit = dict(getattr(row.outcome, "audit", {}) or {})
        except Exception as exc:
            raise RecoveryFailed(f"recovered audit unreadable: {exc}") from exc
        try:
            if row.call.name == "edit" and "changed" not in outcome_audit:
                outcome_audit["changed"] = bool(row.outcome.changed)
            prior = build_recovered_tool_result(
                row.call,
                model_text=row.outcome.model_text,
                audit=outcome_audit,
                presentation=getattr(row.outcome, "presentation", {}),
                canonical=getattr(row.outcome, "canonical", {}),
                truncated=getattr(row.outcome, "truncated", False),
            )
        except RecoveryFailed:
            raise
        except Exception as exc:
            raise RecoveryFailed(f"recovered result rebuild failed: {exc}") from exc
        try:
            record_facts_for_result(session, row.call, prior, ok=bool(row.outcome.ok),
                                    exit_code=row.outcome.exit_code)
        except Exception as exc:
            raise RecoveryFailed(f"recovered facts replay failed: {exc}") from exc


def _build_initial_results(recovered_rows: list[Any]) -> tuple[int, list[Any]]:
    from codey.operations.kernel_recovery import RecoveryFailed

    try:
        from codey.operations.kernel_result import build_recovered_tool_result
    except Exception as exc:
        raise RecoveryFailed(f"recovery helpers unavailable: {exc}") from exc
    resume_start = 1
    initial_results: list[Any] = []
    if not recovered_rows:
        return resume_start, initial_results
    try:
        resume_start = max(int(getattr(r, "turn", 0) or 0) for r in recovered_rows) + 1
        resume_start = max(1, resume_start)
    except Exception as exc:
        raise RecoveryFailed(f"recovered resume turn unreadable: {exc}") from exc
    for row in recovered_rows:
        try:
            audit = dict(getattr(row.outcome, "audit", {}) or {})
            if row.call.name == "edit" and "changed" not in audit:
                audit["changed"] = bool(row.outcome.changed)
            initial_results.append(build_recovered_tool_result(
                row.call,
                model_text=row.outcome.model_text,
                audit=audit,
                presentation=getattr(row.outcome, "presentation", {}),
                canonical=getattr(row.outcome, "canonical", {}),
                truncated=getattr(row.outcome, "truncated", False),
            ))
        except RecoveryFailed:
            raise
        except Exception as exc:
            raise RecoveryFailed(f"recovered initial result rebuild failed: {exc}") from exc
    return resume_start, initial_results


def _entry_recovery(frame: RunFrame, session: Any) -> tuple[dict[str, Any], list[Any], int, list[Any]]:
    """Rebuild recovery state; any failure raises RecoveryFailed (fail-closed).

    Only ``recovery success`` or ``explicit recovery failure`` exist. An
    empty delivered map, a bare ToolResult, or a reset to turn 1 must never
    mask a malformed row: the run stops before any new tool executes.
    """
    from codey.operations.kernel_recovery import RecoveryFailed

    try:
        from codey.operations.recovery import delivered_from_frame
    except Exception as exc:
        raise RecoveryFailed(f"recovery import failed: {exc}") from exc
    if not callable(delivered_from_frame):
        raise RecoveryFailed("recovery builder unavailable")
    try:
        delivered = delivered_from_frame(frame, effect_scope="task")
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovery delivery failed: {exc}") from exc
    raw_rows = list(getattr(frame, "recovered_tool_outcomes", ()) or ())
    _validate_recovered_rows(raw_rows)
    recovered_rows = _sort_recovered_rows(raw_rows)
    _replay_recovered_facts(session, recovered_rows)
    resume_start, initial_results = _build_initial_results(recovered_rows)
    return delivered, recovered_rows, resume_start, initial_results


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
        )
        active_provider = KernelRecordedProvider(active_provider, intent_sink)
    stop_flag = getattr(getattr(deps, "state", None), "run_registry", None)
    stop_flag = getattr(stop_flag, "stop_flag", None)
    return active_provider, intent_sink, stop_flag


def run_entry_kernel(
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
        return _direct_answer_outcome(frame, kind)
    session = _create_entry_session(frame, work, policy, kind)
    try:
        from codey.operations.recovery import record_entry_policy

        record_entry_policy(
            getattr(deps, "runtime_mutations", None),
            session_id=request.session_id, run_id=frame.run_id, policy=policy,
        )
    except Exception:
        pass
    project_path, tool_fns, research_tools = _entry_executors(frame, deps, policy)
    try:
        delivered, _, resume_start, initial_results = _entry_recovery(frame, session)
    except Exception as exc:
        from codey.operations.kernel_recovery import RecoveryFailed as _RecoveryFailed

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
    active_provider, intent_sink, stop_flag = _entry_provider_sink(frame, deps, kind)
    try:
        ignored = tuple(getattr(getattr(config_result, "config", None), "ignored_paths", ()) or ())
    except Exception:
        ignored = ()
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
    )
    # Stash the session + ledger for hybrid quality phases (same fact view).
    try:
        frame.entry_session = session
        frame.entry_research_tools = research_tools
    except Exception:
        pass
    summary = str(result.summary or "")
    receipt = {"display": {"summary": summary[:2000]}}
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


def _direct_answer_outcome(frame: RunFrame, kind: str) -> ModeOutcome:
    request = frame.request
    summary = str(getattr(frame, "handoff", "") or "")
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
