"""Unified ``auto`` execution loop (independent module).

``auto`` 不再经 Ghost LLM 路由。本模块的一次正常首调用同时完成回答与
动作选择：主模型首输出要么是直接回答，要么是受权限检查的动作请求。
控制器只验结构性权限（有无项目、模式是否支持），不做关键词语义判断；
语义服从是模型在本次调用内的职责。

动作标记为纯文本首行（无 JSON、无隐藏通道，解析失败即为普通回答，
不存在泄露问题）：

```text
ACTION: research
PLAN: <一句话计划，可引用用户原话>
```

首输出直接用于回答或执行动作，绝不作为被丢弃的“新路由轮”。
有项目的普通问候不会在动作选定前抢占项目写锁；真正选择编辑类动作后
再由本模块取得相应资源。恢复中的工具结果仍按现有安全恢复路径处理
（调用方在首调用前检查 ``recovered_tool_outcomes`` 并直走 project）。

资源边界（诚实说明）：provider 连接是模型级而非模式级，首调用仍复用
前置准备阶段按基线连好的 provider；模式级独占资源（项目写锁、ledger
模式）为 ``auto`` 延迟到动作选定后。会话准备对象（conversation plan）
是轻量计划，实际窗口初始化仍由各模式入口完成。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from codey.agents.runner import RunResult
from codey.operations.result import ModeOutcome
from codey.runtime.observe.prompt_envelope import record_provider_send_prompt

AUTO_ACTION_KINDS = ("research", "project", "planning_readonly", "review")
AUTO_DIRECT_ANSWER_KIND = "chat"


@dataclass(frozen=True)
class AutoDecision:
    kind: str
    plan: str
    answer: str


def is_auto_request(request: Any) -> bool:
    return str(getattr(request, "intent", "") or "").strip().lower() == "auto"


def build_auto_first_prompt(task: str, *, project: str = "") -> str:
    project_line = (
        "An attached project is available; request ACTION: project only when the "
        "user asks to change project files, ACTION: planning_readonly for "
        "inspect or read-only questions."
        if str(project or "").strip()
        else "No project is attached; never request project, planning, or review actions."
    )
    return (
        "Decide how to handle the user request below in one step.\n"
        "If it is a greeting, question, or anything answerable directly, just answer it.\n"
        "Only when you need fresh external information or attached-project access, "
        "output exactly:\n"
        "ACTION: research|project|planning_readonly|review\n"
        "PLAN: <one-paragraph plan>\n"
        f"{project_line}\n"
        "Research needs no project. Project/planning/review need an attached project.\n"
        "Never emit an ACTION line as a routing probe: when you request an action, "
        "the PLAN is forwarded to that action.\n\n"
        f"User request:\n{task}\n"
    )


def parse_auto_first_output(raw: str) -> AutoDecision:
    text = str(raw or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return AutoDecision(kind=AUTO_DIRECT_ANSWER_KIND, plan="", answer=text)
    first = lines[0].lower()
    if not first.startswith("action:"):
        return AutoDecision(kind=AUTO_DIRECT_ANSWER_KIND, plan="", answer=text)
    kind = first.split(":", 1)[1].strip().lower()
    if kind == "planning":
        kind = "planning_readonly"
    plan_lines = [
        line[5:].strip() if line.lower().startswith("plan:") else line
        for line in lines[1:]
    ]
    plan = "\n".join(plan_lines).strip()
    if kind not in AUTO_ACTION_KINDS:
        return AutoDecision(kind=AUTO_DIRECT_ANSWER_KIND, plan="", answer=text)
    return AutoDecision(kind=kind, plan=plan, answer="")


def strip_action_markers(raw: str) -> str:
    """Fallback display when a requested action is denied: drop marker lines."""
    kept = [
        line for line in str(raw or "").splitlines()
        if not line.strip().lower().startswith(("action:", "plan:"))
    ]
    stripped = "\n".join(kept).strip()
    return stripped or "I could not take that action with the current setup."


def check_auto_action_permitted(
    decision: AutoDecision,
    *,
    project: str = "",
    has_reviewable_diff: Callable[[], bool] | None = None,
    research_available: bool = True,
) -> tuple[bool, str]:
    """Structural permission gate only (no keyword semantics)."""
    if decision.kind == AUTO_DIRECT_ANSWER_KIND:
        return True, ""
    if decision.kind == "research":
        if not research_available:
            return False, "research unavailable"
        return True, ""
    if not str(project or "").strip():
        return False, "project required"
    if decision.kind == "review":
        try:
            if has_reviewable_diff is not None and not bool(has_reviewable_diff()):
                return False, "no reviewable diff"
        except Exception:
            return False, "review check failed"
        return True, ""
    return True, ""


def with_auto_plan(request: Any, plan: str) -> Any:
    """Attach the model's PLAN as a labeled execution hint (never user text).

    The submission's task stays the pristine user request; executors read
    execution_task() while ledger, observations, and snapshots keep task.
    """
    plan_text = str(plan or "").strip()
    if not plan_text:
        return request
    try:
        return replace(request, model_hint=plan_text)
    except Exception:
        return request


@dataclass(frozen=True)
class AutoRunDeps:
    """Callbacks the unified auto loop needs; built by dispatch (no imports)."""

    state: Any
    mode_deps: Any
    config_result: Any
    acquire_writer: Callable[[str], bool]
    release_writer: Callable[[str], None]
    open_ledger_for: Callable[[str], None]
    ghost_directive_fn: Callable[..., Any] | None = None
    ghost_continuity_fn: Callable[..., Any] | None = None
    experiences_fn: Callable[..., str] | None = None
    has_reviewable_diff_fn: Callable[[], bool] | None = None
    research_available: bool = True


def _local_context_text(deps: AutoRunDeps, *, session_id: str, project: str) -> str:
    parts: list[str] = []
    if deps.ghost_directive_fn is not None:
        with contextlib.suppress(Exception):
            parts.append(str(getattr(
                deps.ghost_directive_fn(session_id=session_id), "text", "",
            ) or ""))
    if deps.ghost_continuity_fn is not None:
        with contextlib.suppress(Exception):
            parts.append(str(getattr(
                deps.ghost_continuity_fn(session_id=session_id), "text", "",
            ) or ""))
    return "\n\n".join(part for part in parts if part.strip())


def run_auto_mode(frame: Any, work: Any, hooks: Any, deps: AutoRunDeps) -> ModeOutcome:
    """Run one unified auto turn: first normal call decides answer vs action."""
    from codey.operations.prompting import join_local_contexts, prepend_ghost_directive

    state = deps.state
    request = frame.request
    project_text = str(getattr(frame, "project_text", "") or "")
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    ghost_context = join_local_contexts(
        _local_context_text(
            deps, session_id=request.session_id, project=request.project or "",
        ),
        "",
    )
    experiences = ""
    if deps.experiences_fn is not None:
        try:
            experiences = deps.experiences_fn(
                session_id=request.session_id,
                project=request.project or "",
                query=request.task,
            )
        except Exception:
            experiences = ""
    prompt = prepend_ghost_directive(
        build_auto_first_prompt(request.task, project=project_text), ghost_context,
    )
    if experiences.strip():
        prompt = f"{prompt}\n\n{experiences.strip()}"
    if frame.fresh_chat:
        # Same window discipline as chat: the decision call runs in a fresh
        # window. Action paths reset again below so mode runners start clean
        # and the ACTION scaffolding never pollutes their history.
        frame.provider.new_chat()
    with contextlib.suppress(Exception):
        record_provider_send_prompt(
            frame.trace,
            name="auto_outbound_prompt",
            text=prompt,
            purpose="auto first call sent to provider",
            source_ref="provider_send:auto",
            capability_id="auto_runner",
        )
    # The first output is never replaced by a second routing round, and a dead
    # first call is never retried here: provider failure propagates to the
    # existing error settlement instead of re-issuing the just-abandoned slow
    # request through a baseline runner.
    raw = frame.provider.send(prompt)
    decision = parse_auto_first_output(raw)
    if decision.kind == AUTO_DIRECT_ANSWER_KIND:
        return _finish_auto_answer(frame, state, prompt, decision.answer)
    permitted, _reason = check_auto_action_permitted(
        decision,
        project=project_text,
        has_reviewable_diff=deps.has_reviewable_diff_fn,
        research_available=deps.research_available,
    )
    if not permitted:
        return _finish_auto_answer(frame, state, prompt, strip_action_markers(raw))
    frame.request = with_auto_plan(request, decision.plan)
    frame.task_kind = decision.kind
    deps.open_ledger_for(decision.kind)
    # The ACTION scaffolding must not leak into the mode runner's window: a
    # failed reset propagates to error settlement instead of continuing with
    # inherited ACTION history.
    frame.provider.new_chat()
    frame.fresh_chat = False
    if decision.kind == "project":
        if not deps.acquire_writer(project_text):
            summary = "另一个任务正在写该项目，稍后重试。"
            return ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": summary,
                "stop_reason": "stopped",
                "turns": 1,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "project",
            })
        try:
            return deps.mode_deps.project(
                frame, work, hooks, config_result=deps.config_result,
            )
        finally:
            with contextlib.suppress(Exception):
                deps.release_writer(project_text)
    if decision.kind == "research":
        return deps.mode_deps.research(frame, hooks)
    if decision.kind == "planning_readonly":
        return deps.mode_deps.planning(frame, work, config_result=deps.config_result)
    if decision.kind == "review":
        return deps.mode_deps.review(frame)
    return _finish_auto_answer(frame, state, prompt, strip_action_markers(raw))


def _finish_auto_answer(frame: Any, state: Any, prompt: str, answer: str) -> ModeOutcome:
    """Record a direct auto answer exactly like a chat turn (no second call)."""
    request = frame.request
    reply = str(answer or "")
    if frame.fresh_chat:
        frame.conversation.begin_window(frame.provider_id, "chat")
    with contextlib.suppress(Exception):
        state.set_provider_session(frame.provider_id, request.session_id)
    frame.conversation.record_exchange(
        prompt,
        reply,
        replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            blocker="",
            latest_user=request.task,
            latest_reply=reply,
        ),
    )
    result = RunResult(reply, "done", 1)
    return ModeOutcome({
        "type": "task_done",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "summary": result.summary,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "max_turns": request.max_turns,
        "provider": frame.provider_id,
        "mode": "chat",
    }, display=({
        "type": "reply",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "text": reply,
    },))


__all__ = [
    "AUTO_ACTION_KINDS",
    "AUTO_DIRECT_ANSWER_KIND",
    "AutoDecision",
    "AutoRunDeps",
    "build_auto_first_prompt",
    "check_auto_action_permitted",
    "is_auto_request",
    "parse_auto_first_output",
    "run_auto_mode",
    "strip_action_markers",
    "with_auto_plan",
]
