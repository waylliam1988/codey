"""Production entry for the unified task kernel (operations layer).

``run_unified_mode`` builds the immutable policy, the kernel session, and the
real project/research executors, then drives the single ``run_task_kernel``
loop and converts the outcome to the existing ``ModeOutcome`` shape (UI/SSE
events, trace, and ledger projections stay unchanged). Auto intent is decided
by the first model call inside the same run; its PLAN only narrows execution
scope and never widens authorization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.result import ModeOutcome
from codey.task.model import execution_task

_UNIFIED_KINDS = frozenset({"project", "research", "hybrid", "planning", "planning_readonly", "readonly"})


def _unified_kind(task_kind: object) -> str:
    kind = str(task_kind or "").strip().lower()
    if kind in _UNIFIED_KINDS:
        return "planning" if kind in {"planning_readonly", "readonly"} else kind
    return "project" if kind in {"auto", "unified"} else kind


def build_unified_policy(request: Any, task_kind: object) -> Any:
    from codey.operations.task_loop import policy_for_dispatch

    kind = _unified_kind(task_kind)
    strict = kind == "research" or bool(getattr(request, "strict_research", False) is True)
    return policy_for_dispatch(request, kind, strict_research=strict)


def _build_research_tools(deps: Any, *, session_id: str, project: str) -> Any | None:
    knowledge_store = getattr(deps, "knowledge_store", None)
    if knowledge_store is None:
        return None
    search_factory = getattr(deps, "search_factory", None)
    if not callable(search_factory):
        from codey.operations.research_flow import default_research_search_provider

        search_factory = default_research_search_provider
    try:
        search = search_factory()
    except Exception:
        return None
    try:
        from codey.knowledge.changes import KnowledgeChanges

        changes = KnowledgeChanges(root=getattr(knowledge_store, "root", "."))
    except Exception:
        return None
    try:
        from codey.research.tools import ResearchTools

        return ResearchTools(
            search=search,
            store=knowledge_store,
            changes=changes,
            diagnostics=None,
            session_id=session_id,
            project=project,
        )
    except Exception:
        return None


def run_unified_mode(
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: Any,
    *,
    task_kind: str = "",
    config_result: Any = None,
) -> ModeOutcome:
    from codey.operations.task_loop import (
        TaskSession,
        _record_facts_for_result,
        apply_auto_plan,
        run_task_kernel,
    )
    from codey.runtime.core.models import ToolResult

    request = frame.request
    kind = _unified_kind(task_kind or frame.task_kind)
    policy = build_unified_policy(request, kind)
    if policy is None:
        raise RuntimeError("unified policy unavailable")
    # Auto: first model call in the same run decides direct answer vs action.
    # The PLAN narrows scope only; authorization stays the entry snapshot.
    auto_plan = ""
    if str(getattr(request, "intent", "") or "").strip().lower() == "auto":
        auto_plan = _decide_auto(frame)
        if auto_plan == "__direct__":
            return _direct_answer_outcome(frame, kind)
        if auto_plan:
            policy = apply_auto_plan(policy, auto_plan)
    session = TaskSession(
        policy=policy,
        task_kind=kind,
        project=frame.project_text,
        max_turns=max(1, int(getattr(request, "max_turns", 8) or 8)),
        task_text=execution_task(request),
        handoff=str(getattr(frame, "handoff", "") or ""),
        project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
    )
    # Seed the session workspace identity from the real outer evidence so
    # verification facts can match the current files (never synthesized).
    try:
        ev = getattr(work, "evidence", None)
        if ev is not None:
            session.set_workspace_state(getattr(ev, "workspace_revision", 0),
                                        getattr(ev, "workspace_fingerprint", ""))
    except Exception:
        pass
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
    delivered = _delivered_from_recovery(frame, effect_scope="unified")
    recovered_rows = sorted(
        list(getattr(frame, "recovered_tool_outcomes", ()) or ()),
        key=lambda r: (int(getattr(r, "turn", 0) or 0), int(getattr(r, "tool_index", 0) or 0)),
    )
    for row in recovered_rows:
        prior = ToolResult(
            call=row.call, model_text=row.outcome.model_text,
            audit={"changed": bool(row.outcome.changed)} if row.call.name == "edit" else {},
        )
        _record_facts_for_result(session, row.call, prior, ok=bool(row.outcome.ok),
                                 exit_code=row.outcome.exit_code)
    resume_start = 1
    initial_results: list[ToolResult] = []
    if recovered_rows:
        try:
            resume_start = max(int(getattr(r, "turn", 0) or 0) for r in recovered_rows) + 1
            resume_start = max(1, resume_start)
        except Exception:
            resume_start = 1
        for row in recovered_rows:
            initial_results.append(ToolResult(call=row.call, model_text=row.outcome.model_text))
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
    result = run_task_kernel(
        session,
        provider=active_provider,
        executors={},
        run_id=frame.run_id,
        effect_scope="unified",
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
    )
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
        try:
            raw = provider.send(prompt, timeout=None)
        except TypeError as exc:
            if "timeout" not in str(exc):
                raise
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
    frame.task_kind = kind if kind in _UNIFIED_KINDS else "project"
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
    """Single task entry: auth + completion for project/research/hybrid/planning.

    All kinds share one TaskSession/tool loop via ``run_task_kernel``; the
    ResearchPipeline and project review/repair stay as strategy phases
    scheduled here (project/research/planning delegate to their flows which
    already drive the same kernel, hybrid runs one alternating session plus
    ledger/review projections so outer quality is preserved).
    """
    kind = _unified_kind(task_kind or getattr(frame, "task_kind", ""))
    if kind == "hybrid":
        outcome = run_unified_mode(frame, work, hooks, deps, task_kind="hybrid", config_result=config_result)
        # Preserve outer quality signals the old two-phase hybrid carried:
        # research ledger projection + review trigger live alongside the
        # single-session outcome (same SSE/events, same final display shape).
        try:
            return _enrich_hybrid_outcome(frame, work, outcome)
        except Exception:
            return outcome
    if kind == "project" and callable(run_project):
        return run_project(frame, work, hooks, config_result=config_result)
    if kind == "research" and callable(run_research):
        return run_research(frame, hooks)
    if kind == "planning" and callable(run_planning):
        return run_planning(frame, work, config_result=config_result)
    # Fallback: direct unified kernel for kinds without a dedicated flow.
    return run_unified_mode(frame, work, hooks, deps, task_kind=kind, config_result=config_result)


def _enrich_hybrid_outcome(frame: RunFrame, work: RunWork, outcome: ModeOutcome) -> ModeOutcome:
    """Attach research ledger + review hints without changing the single-session result."""
    try:
        event = dict(outcome.event or {})
    except Exception:
        return outcome
    # Keep the single-session summary/stop_reason/turns; add ledger refs when
    # the work evidence already carries them (same sources the old pipeline
    # projected). SSE events were already emitted via hooks.on_event in the
    # kernel, so the display shape stays ModeOutcome-compatible.
    try:
        evidence = getattr(work, "evidence", None)
        if evidence is not None:
            rendered = None
            try:
                rendered = evidence.render_for_review()
            except Exception:
                rendered = None
            if rendered:
                receipt = dict(event.get("receipt") or {})
                display = dict(receipt.get("display") or {})
                if "evidence" not in display:
                    display["evidence"] = str(rendered)[:2000]
                    receipt["display"] = display
                    event["receipt"] = receipt
    except Exception:
        pass
    try:
        return ModeOutcome(event)
    except Exception:
        return outcome


def _delivered_from_recovery(frame: RunFrame, *, effect_scope: str = "") -> dict[str, Any]:
    delivered: dict[str, Any] = {}
    try:
        from codey.operations.task_loop import turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult
    except Exception:
        return delivered
    for item in getattr(frame, "recovered_tool_outcomes", ()) or ():
        try:
            call = getattr(item, "call", None)
            outcome = getattr(item, "outcome", None)
            turn = int(getattr(item, "turn", 0) or 0)
            index = int(getattr(item, "tool_index", 0) or 0)
            identity_ref = f"{frame.run_id}:{effect_scope}" if effect_scope else frame.run_id
            identity = turn_effect_id(identity_ref, turn, index)
            delivered[identity] = ToolResult(
                call=ToolCall(str(getattr(call, "name", "") or ""),
                              dict(getattr(call, "args", {}) or {}),
                              str(getattr(call, "call_id", "") or "")),
                model_text=str(getattr(outcome, "model_text", "") or ""),
            )
        except Exception:
            continue
    return delivered


__all__ = ["build_unified_policy", "run_task_mode", "run_unified_mode"]
