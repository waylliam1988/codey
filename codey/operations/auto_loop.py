"""Unified ``auto`` execution loop (independent module).

``auto`` 不再经 Ghost LLM 路由。本模块的一次正常首调用同时完成回答与
动作选择：主模型首输出要么是直接回答，要么是受权限检查的动作请求。
控制器只验结构性权限（有无项目、模式是否支持），不做关键词语义判断；
语义服从是模型在本次调用内的职责。

网页文本协议的动作标记为纯文本首行（无 JSON、无隐藏通道，解析失败即为普通回答，
不存在泄露问题）：

```text
ACTION: research
PLAN: <一句话计划，可引用用户原话>
```

首输出直接用于回答或执行动作，绝不作为被丢弃的“新路由轮”。
原生 API 首调用直接使用冻结且经过授权筛选的工具声明；结构化调用及
其原始快照交给同一任务内核，计入同一轮次预算，不再次生成首轮。
有项目的普通问候不会在动作选定前抢占项目写锁；真正选择编辑类动作后
再由本模块取得相应资源。恢复中的工具结果仍按现有安全恢复路径处理
（调用方在首调用前检查待交付结果与历史事实，直接恢复原任务）。

资源边界（诚实说明）：provider 连接是模型级而非模式级，首调用仍复用
前置准备阶段按基线连好的 provider；模式级独占资源（项目写锁、ledger
模式）为 ``auto`` 延迟到动作选定后。工具任务沿用同一个会话、授权、
完成要求与累计预算；review 是独立报告策略，不建立工具循环。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from codey.operations.provider_session import provider_reasoning
from codey.operations.result import ModeOutcome
from codey.runtime.observe.events import RunEvent
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
    return replace(request, model_hint=plan_text)


@dataclass(frozen=True)
class AutoRunDeps:
    """Callbacks the unified auto loop needs; built by dispatch (no imports)."""

    state: Any
    acquire_writer: Callable[[str], bool]
    release_writer: Callable[[str], None]
    open_ledger_for: Callable[[str], None]
    ghost_directive_fn: Callable[..., Any] | None = None
    ghost_continuity_fn: Callable[..., Any] | None = None
    experiences_fn: Callable[..., str] | None = None
    has_reviewable_diff_fn: Callable[[], bool] | None = None
    research_available: bool = True
    continue_task: Callable[..., ModeOutcome] | None = None
    review_task: Callable[[Any], ModeOutcome] | None = None
    prepare_native_provider: Callable[[Any], Any] | None = None


def _local_context_text(deps: AutoRunDeps, *, session_id: str, project: str) -> str:
    parts: list[str] = []
    if deps.ghost_directive_fn is not None:
        with contextlib.suppress(Exception):
            parts.append(str(getattr(
                deps.ghost_directive_fn(session_id=session_id, project=project), "text", "",
            ) or ""))
    if deps.ghost_continuity_fn is not None:
        with contextlib.suppress(Exception):
            parts.append(str(getattr(
                deps.ghost_continuity_fn(session_id=session_id, project=project), "text", "",
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
    from codey.operations.kernel_transport import provider_uses_native

    if provider_uses_native(frame.provider, provider_id=frame.provider_id):
        return _run_native_auto(frame, work, hooks, deps, ghost_context, experiences)
    if frame.fresh_chat:
        # Open the one task window. Tool continuation records this exchange
        # and keeps the window and first-turn budget.
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
    reasoning = provider_reasoning(frame.provider, raw)
    if reasoning:
        hooks.on_event(RunEvent("reasoning", turn=1, reasoning=reasoning))
    decision = parse_auto_first_output(raw)
    if decision.kind == AUTO_DIRECT_ANSWER_KIND:
        return _finish_auto_answer(frame, work, hooks, deps, state, prompt, decision.answer)
    permitted, _reason = check_auto_action_permitted(
        decision,
        project=project_text,
        has_reviewable_diff=deps.has_reviewable_diff_fn,
        research_available=deps.research_available,
    )
    if not permitted:
        return _finish_auto_answer(frame, work, hooks, deps, state, prompt, strip_action_markers(raw))
    _record_direct_exchange(frame, state, prompt, raw)
    frame.request = with_auto_plan(request, decision.plan)
    if decision.kind == "review":
        if deps.review_task is None:
            return _direct_outcome(frame, "Review is unavailable.", stop_reason="blocked")
        deps.open_ledger_for("review")
        return deps.review_task(frame)
    from codey.operations.task_entry import build_task_policy_for_entry, start_task_session

    policy = frame.entry_policy
    if policy is None:
        policy = build_task_policy_for_entry(frame.request, frame.task_kind)
        frame.entry_policy = policy
    session = start_task_session(frame, work, policy, frame.task_kind)
    session.turn = max(session.turn, 1)
    followup = "Continue the original authorized task using the shared tool protocol."
    if decision.plan:
        followup += f"\nModel plan: {decision.plan}"
    return _continue_direct_candidate(frame, work, hooks, deps, followup)


def _run_native_auto(frame: Any, work: Any, hooks: Any, deps: AutoRunDeps,
                     ghost_context: str, experiences: str) -> ModeOutcome:
    """Receive once with authorized tools; execute that same turn in the kernel."""
    from codey.operations.kernel_preparation import prepare_kernel_turn
    from codey.operations.kernel_protocol import InitialNativeTurn
    from codey.operations.provider_session import reply_text_for_accounting
    from codey.operations.task_entry import build_task_policy_for_entry, start_task_session
    from codey.operations.task_guidance import task_guidance_for_policy
    from codey.providers.base import TurnFinish, tools_from_specs
    from codey.task.model import execution_task

    policy = frame.entry_policy or build_task_policy_for_entry(frame.request, frame.task_kind)
    frame.entry_policy = policy
    session = start_task_session(frame, work, policy, frame.task_kind)
    prepared = prepare_kernel_turn(
        session, user_task=execution_task(frame.request),
        context_text="\n\n".join(p for p in (ghost_context, experiences) if p.strip()),
        native=True, task_guidance=task_guidance_for_policy(policy),
    )
    snapshot, prompt = prepared.snapshot, prepared.prompt
    prompt += "\nIf the request can be answered directly without tools, answer it directly."
    provider = deps.prepare_native_provider(frame) if deps.prepare_native_provider else frame.provider
    if frame.fresh_chat:
        provider.new_chat()
    with contextlib.suppress(Exception):
        record_provider_send_prompt(frame.trace, name="auto_outbound_prompt", text=prompt,
                                    purpose="authorized native auto first turn",
                                    source_ref="provider_send:auto", capability_id="auto_runner")
    reply = provider.send_turn(prompt, tools_from_specs(snapshot.frozen_specs))
    if not reply.tool_calls and reply.finish is TurnFinish.COMPLETE:
        if reply.reasoning:
            hooks.on_event(RunEvent("reasoning", turn=1, reasoning=reply.reasoning))
        return _finish_auto_answer(frame, work, hooks, deps, deps.state, prompt, reply.text)
    _record_direct_exchange(frame, deps.state, prompt, reply_text_for_accounting(reply))
    frame.entry_initial_turn = InitialNativeTurn(reply, snapshot, prompt, owner_session=session)
    # session.turn stays zero: the kernel consumes the received turn as turn 1,
    # including its truncation/cancellation checks and original frozen specs.
    return _continue_direct_candidate(frame, work, hooks, deps, "")


def _record_direct_exchange(frame: Any, state: Any, prompt: str, reply: str) -> None:
    request = frame.request
    if frame.fresh_chat:
        frame.conversation.begin_window(frame.provider_id, "chat", frame.project_text)
    frame.fresh_chat = False
    setter = getattr(state, "set_provider_session", None)
    if callable(setter):
        setter(frame.provider_id, request.session_id)
    frame.conversation.record_exchange(
        prompt, reply,
        replace(frame.conversation.snapshot, provider_id=frame.provider_id,
                blocker="", latest_user=request.task, latest_reply=reply),
    )


def _direct_outcome(frame: Any, reason: str, *, stop_reason: str, proof: Any = None) -> ModeOutcome:
    receipt = {"display": {"summary": reason[:2000]}}
    if proof is not None:
        receipt["completion_proof"] = proof.to_payload()
    return ModeOutcome({
        "type": "task_done", "run_id": frame.run_id,
        "session_id": frame.request.session_id, "summary": reason,
        "stop_reason": stop_reason, "turns": 1, "max_turns": frame.request.max_turns,
        "provider": frame.provider_id, "mode": "chat", "receipt": receipt,
    }, display=({"type": "reply", "run_id": frame.run_id,
                 "session_id": frame.request.session_id, "text": reason},) if stop_reason == "done" else ())


def _close_initial_turn(frame: Any, deps: AutoRunDeps, reason: str, stop_reason: str) -> ModeOutcome:
    from codey.operations.kernel_transport import close_native_reply
    from codey.providers.base import tools_from_specs

    initial, frame.entry_initial_turn = getattr(frame, "entry_initial_turn", None), None
    if initial is not None:
        provider = deps.prepare_native_provider(frame) if deps.prepare_native_provider else frame.provider
        try:
            close_native_reply(provider, initial.reply, f"{reason}; call not executed",
                               declared_tools=tools_from_specs(initial.snapshot.frozen_specs))
        except Exception as exc:
            return _direct_outcome(frame, f"Result delivery failed: {type(exc).__name__}: {exc}",
                                   stop_reason="provider_failure")
    return _direct_outcome(frame, reason, stop_reason=stop_reason)


def _continue_direct_candidate(frame: Any, work: Any, hooks: Any, deps: AutoRunDeps, followup: str) -> ModeOutcome:
    session = frame.entry_session
    if session.turn >= session.max_turns:
        return _close_initial_turn(frame, deps, followup, "max_turns")
    if deps.continue_task is None:
        return _close_initial_turn(frame, deps, followup, "blocked")
    deps.open_ledger_for(session.task_kind)
    project = frame.project_text
    leased = False
    if project and (session.policy.allows("project.write") or session.policy.allows("shell.approval")):
        try:
            leased = deps.acquire_writer(project) is True
        except Exception as exc:
            return _close_initial_turn(frame, deps, f"Project writer lease failed: {type(exc).__name__}: {exc}",
                                       "blocked")
        if not leased:
            return _close_initial_turn(frame, deps, "Project writer is busy; retry later.", "stopped")
    try:
        return deps.continue_task(frame, work, hooks, followup=followup)
    finally:
        if leased:
            deps.release_writer(project)


def _finish_auto_answer(
    frame: Any, work: Any, hooks: Any, deps: AutoRunDeps, state: Any, prompt: str, answer: str,
) -> ModeOutcome:
    """A direct candidate either completes or continues the same task kernel."""
    from codey.operations.task_entry import evaluate_direct_answer_candidate

    reply = str(answer or "")
    _record_direct_exchange(frame, state, prompt, reply)
    try:
        verdict = evaluate_direct_answer_candidate(frame, reply, work=work)
    except Exception as exc:
        return _direct_outcome(frame, f"Completion gate failed: {type(exc).__name__}: {exc}", stop_reason="blocked")
    if verdict is None or type(getattr(verdict, "complete", None)) is not bool:
        return _direct_outcome(frame, "Completion gate returned an invalid verdict.", stop_reason="blocked")
    session = frame.entry_session
    session.turn = max(session.turn, 1)
    if verdict.complete:
        return _direct_outcome(frame, verdict.final_text or reply, stop_reason="done", proof=verdict.proof)
    return _continue_direct_candidate(
        frame, work, hooks, deps, verdict.followup or "Complete the missing task requirements.",
    )


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
