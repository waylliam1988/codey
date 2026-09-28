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
    return "project" if kind == "auto" else kind


def build_unified_policy(request: Any, task_kind: object) -> Any:
    from codey.operations.task_kernel import policy_for_dispatch

    kind = _unified_kind(task_kind)
    strict = kind == "research" or bool(getattr(request, "strict_research", False) is True)
    return policy_for_dispatch(request, kind, strict_research=strict)


def _narrow_for_auto(policy: Any, request: Any) -> tuple[Any, str]:
    try:
        from codey.operations.auto_loop import is_auto_request
    except Exception:
        return policy, ""
    try:
        if not bool(is_auto_request(request)):
            return policy, ""
    except Exception:
        return policy, ""
    return policy, ""


def _build_research_tools(deps: Any, *, session_id: str, project: str) -> Any | None:
    knowledge_store = getattr(deps, "knowledge_store", None)
    if knowledge_store is None:
        return None
    search_factory = getattr(deps, "search_factory", None)
    if not callable(search_factory):
        return None
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
    from codey.operations.task_kernel import TaskSession, apply_auto_plan, run_task_kernel

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
    )
    project_path: Path | None = None
    if frame.project_text:
        candidate = Path(frame.project_text).expanduser()
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
    delivered = _delivered_from_recovery(frame)
    stop_flag = getattr(getattr(deps, "state", None), "run_registry", None)
    stop_flag = getattr(stop_flag, "stop_flag", None)
    result = run_task_kernel(
        session,
        provider=frame.provider,
        executors={},
        run_id=frame.run_id,
        provider_id=frame.provider_id,
        project_path=project_path,
        tool_fns=tool_fns,
        research_tools=research_tools,
        user_task=execution_task(request),
        stop_flag=stop_flag,
        delivered=delivered or None,
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
        raw = provider.send(prompt, timeout=None)
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


def _delivered_from_recovery(frame: RunFrame) -> dict[str, Any]:
    delivered: dict[str, Any] = {}
    try:
        from codey.operations.task_kernel import turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult
    except Exception:
        return delivered
    for item in getattr(frame, "recovered_tool_outcomes", ()) or ():
        try:
            call = getattr(item, "call", None)
            outcome = getattr(item, "outcome", None)
            turn = int(getattr(item, "turn", 0) or 0)
            index = int(getattr(item, "tool_index", 0) or 0)
            identity = turn_effect_id(frame.run_id, turn, index)
            delivered[identity] = ToolResult(
                call=ToolCall(str(getattr(call, "name", "") or ""),
                              dict(getattr(call, "args", {}) or {}),
                              str(getattr(call, "call_id", "") or "")),
                model_text=str(getattr(outcome, "model_text", "") or ""),
            )
        except Exception:
            continue
    return delivered


__all__ = ["build_unified_policy", "run_unified_mode"]
