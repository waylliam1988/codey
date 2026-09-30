"""Read-only project audit advisors on the single shared model tool loop.

Execution lives here (operations layering: agents must not import
operations). Prompts, read-only scanners, and advice types stay in
``codey.agents.consensus``. Each advisor runs a read-only sub-session
(a subset of the parent authorization) through the same
``run_task_kernel``; findings project back to the caller as text.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from codey.agents.consensus import (
    MAX_CONSENSUS_ADVISORS,
    PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT,
    PROJECT_AUDIT_MAX_REPORT_CHARS,
    PROJECT_AUDIT_MAX_TURNS,
    ConsensusAdvice,
    _audit_visible_entries,
    _clip,
    _execute_read_only_call,
    _provider_send_ref,
    _trace_model_prompt,
    _trace_warning,
    advisor_ids,
    render_project_audit_prompt,
)
from codey.operations.provider_session import DeadlineProvider
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import controls as provider_controls
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult

_AUDIT_RUNTIME_NAMES = {
    "list_dir": "ls",
    "read_file": "read",
    "grep": "search",
    "find_references": "references",
}


def _audit_kernel_executors(project_path: Path) -> dict[str, object]:
    """Read-only adapter: canonical kernel calls share the audit tool bounds."""

    def run(call: ToolCall) -> ToolResult:
        from codey.toolchain.runtime import ToolOutcome

        runtime_name = _AUDIT_RUNTIME_NAMES.get(call.name, call.name)
        try:
            outcome = _execute_read_only_call(
                project_path,
                ToolCall(runtime_name, dict(call.args or {}), call.call_id),
            )
        except Exception as exc:
            outcome = ToolOutcome.error(str(exc))
        return ToolResult(
            call=call,
            model_text=outcome.model_text,
            truncated=outcome.truncated,
            presentation=dict(outcome.presentation),
            audit=dict(outcome.audit),
            canonical=dict(outcome.canonical),
        )

    return {
        "list_dir": run,
        "read_file": run,
        "grep": run,
        "find_references": run,
    }


def run_project_audit_advisor(
    provider: Any,
    project: str | Path,
    task: str,
    *,
    parent_policy: TaskPolicy,
    context: str = "",
    max_turns: int = PROJECT_AUDIT_MAX_TURNS,
    trace_recorder: object | None = None,
    advisor_id: str = "",
) -> str:
    """Run one read-only audit through the single shared model tool loop.

    只读子会话（父授权的子集：project.read + control），审计特有的路径
    筛选与扫描预算保留在具体工具适配器中；解析/执行/交付/completion 全走
    统一内核，本函数不再自建循环。
    """
    if not parent_policy.allows("project.read"):
        return ""
    project_path = Path(project).expanduser().resolve()
    initial_listing = _audit_visible_entries(project_path, ".").model_text
    prompt = render_project_audit_prompt(
        task=task,
        context=context,
        initial_listing=initial_listing,
    )
    _trace_model_prompt(
        trace_recorder,
        "project_audit_prompt",
        prompt,
        purpose="project audit prompt sent to provider",
        source_ref=_provider_send_ref("project_audit", advisor_id),
    )
    policy = TaskPolicy(grants=frozenset(
        grant for grant in ("control", "project.read") if parent_policy.allows(grant)
    ))
    session = TaskSession(
        policy=policy,
        task_kind="readonly",
        project=str(project_path),
        max_turns=max(1, int(max_turns or PROJECT_AUDIT_MAX_TURNS)),
        task_text=prompt,
    )
    deadline = time.monotonic() + PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT
    run_id = f"audit-{uuid4().hex}"
    stop = threading.Event()
    timer = threading.Timer(
        max(0.1, deadline - time.monotonic()), stop.set,
    )
    timer.daemon = True
    timer.start()
    try:
        with provider_controls.suppress_assistance():
            outcome = run_task_kernel(
                session,
                provider=DeadlineProvider(provider, deadline),
                executors=_audit_kernel_executors(project_path),
                run_id=run_id,
                effect_scope="audit",
                project_path=project_path,
                user_task=prompt,
                stop_flag=stop,
                completion_context={
                    "run_id": run_id,
                    "task": task,
                    "question": task,
                    "project": str(project_path),
                },
            )
    finally:
        timer.cancel()
    if not outcome.completed:
        return ""
    report = _clip(str(outcome.summary or ""), PROJECT_AUDIT_MAX_REPORT_CHARS)
    _trace_model_prompt(
        trace_recorder,
        "project_audit_result",
        report,
        purpose="project audit report returned by provider",
        source_ref=_provider_send_ref("project_audit_result", advisor_id),
    )
    return report


def run_project_audit(
    *,
    parent_policy: TaskPolicy,
    project: str | Path,
    selected_provider_id: str,
    task: str,
    provider_ids: Sequence[str],
    provider_labels: Mapping[str, str],
    availability: Callable[[], Mapping[str, bool]],
    connect_existing: Callable[[str], object],
    clear_provider_session: Callable[[str], None] | None = None,
    context: str = "",
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
    trace_recorder: object | None = None,
) -> tuple[ConsensusAdvice, ...]:
    cancellation.check()
    if not parent_policy.allows("project.read"):
        return ()
    try:
        statuses = dict(availability())
    except Exception:
        statuses = {}
    candidates = advisor_ids(
        selected_provider_id,
        statuses,
        provider_ids,
        max_advisors=max_advisors,
    )
    reports: list[ConsensusAdvice] = []
    for advisor_id in candidates:
        cancellation.check()
        advisor = None
        try:
            advisor = connect_existing(advisor_id)
            if clear_provider_session is not None:
                clear_provider_session(advisor_id)
            advisor.new_chat()
            text = run_project_audit_advisor(
                advisor,
                project,
                task,
                parent_policy=parent_policy,
                context=context,
                trace_recorder=trace_recorder,
                advisor_id=advisor_id,
            )
            if text.strip():
                reports.append(ConsensusAdvice(
                    advisor_id,
                    provider_labels.get(advisor_id, advisor_id),
                    text,
                ))
        except cancellation.TaskCancelled:
            raise
        except Exception:
            _trace_warning(trace_recorder, "project_audit_advisor_failed", advisor_id)
            continue
        finally:
            if advisor is not None:
                with contextlib.suppress(Exception):
                    advisor.close()
    return tuple(reports)


__all__ = ["run_project_audit", "run_project_audit_advisor"]
