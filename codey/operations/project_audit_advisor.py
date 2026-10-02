"""Read-only project audit advisors on the single shared model tool loop.

Execution, audit prompts, and advisor budgets live here (operations
layering: agents must not import operations). Scanning tools live in
``codey.agents.project_audit_tools``. Each advisor runs a read-only
sub-session (a subset of the parent authorization) through the same
``run_task_kernel``; findings project back to the caller as text.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from codey.agents.consensus import (
    MAX_CONSENSUS_ADVISORS,
    MAX_CONTEXT_CHARS,
    ConsensusAdvice,
    _clip,
    _provider_send_ref,
    _trace_model_prompt,
    _trace_warning,
    advisor_ids,
)
from codey.agents.project_audit_tools import execute_read_only_call, visible_entries
from codey.operations.provider_session import DeadlineProvider
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import controls as provider_controls
from codey.providers.base import ChatProvider
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult

PROJECT_AUDIT_MAX_TURNS = 4
PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT = 180.0
PROJECT_AUDIT_MAX_REPORT_CHARS = 4_000

READ_ONLY_AUDIT_PROMPT = """\
You are a private read-only project reviewer for a local assistant.
You cannot edit files, run commands, ask shell approval, browse, or access
anything outside the project. The local runner executes read-only tools for you.

Every reply MUST be exactly one JSON object with no other text:

{"tool":"<name>","args":{...}}

Available read-only tools:

  {"tool":"list_dir","args":{"path":"."}}
    List files in a directory.

  {"tool":"read_file","args":{"path":"app.py"}}
  {"tool":"read_file","args":{"path":"app.py","offset":301,"limit":300}}
    Read one file. Large files are returned in complete-line pages.

  {"tool":"read_files","args":{"paths":["a.py","b.py"]}}
    Read up to 8 files in one step. Do not nest it inside parallel.

  {"tool":"grep","args":{"query":"login","path":"."}}
    Search file contents for case-insensitive literal text before reading when
    the location is unknown. Regex is not supported.

  {"tool":"find_references","args":{"symbol":"createRouter","path":"."}}
    Find bounded lexical reference hints. This is not semantic resolution; use
    read_file before editing or citing exact code.

  {"tool":"parallel","args":{"calls":[{"tool":"grep","args":{"query":"TODO","path":"."}},{"tool":"list_dir","args":{"path":"."}}]}}
    Batch at most 4 independent read-only list_dir, read_file, or grep calls.

  {"tool":"done","args":{"summary":"structured project review findings"}}
    Finish your private review.

Rules:
  - Use only list_dir, read_file, read_files, grep, find_references, parallel, or done.
  - Never call edit, write, run, shell, restore, approve, or any native website tool.
  - find_references output is lexical reference hints only, not semantic resolution.
  - Inspect only files that seem relevant. Keep the review bounded.
  - Report concrete bug risks, architecture concerns, and useful improvement ideas.
  - Include paths as evidence when possible.
  - If you see no concrete issue, say so directly.
  - Do not mention hidden advisors, voting, MoA, or consensus.
"""


def render_project_audit_prompt(
    *,
    task: str,
    context: str = "",
    initial_listing: str = "",
) -> str:
    parts = [
        READ_ONLY_AUDIT_PROMPT,
        "",
        "Initial listing:",
        _clip(initial_listing, 4_000) or "(empty)",
        "",
        "User request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    return "\n".join(parts)


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
            outcome = execute_read_only_call(
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
    initial_listing = visible_entries(project_path, ".").model_text
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
                request=KernelRunRequest(
                    transport=KernelTransportDeps(
                        provider=DeadlineProvider(provider, deadline),
                        run_id=run_id,
                        effect_scope="audit",
                        user_task=prompt,
                        stop_flag=stop,
                    ),
                    execution=KernelExecutionDeps(
                    executors=cast(Mapping[str, Callable[[Any], Any]], _audit_kernel_executors(project_path)),
                        project_path=project_path,
                    ),
                    observation=KernelObservationDeps(
                        completion_context={
                            "run_id": run_id,
                            "task": task,
                            "question": task,
                            "project": str(project_path),
                        },
                    ),
                ),
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
    connect_existing: Callable[[str], ChatProvider],
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
        advisor: ChatProvider | None = None
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


__all__ = [
    "PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT",
    "PROJECT_AUDIT_MAX_REPORT_CHARS",
    "PROJECT_AUDIT_MAX_TURNS",
    "READ_ONLY_AUDIT_PROMPT",
    "render_project_audit_prompt",
    "run_project_audit",
    "run_project_audit_advisor",
]
