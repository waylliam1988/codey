"""Task-facing data model.

This module is the boundary between user-visible task submission protocols and
the internal runtime.  It deliberately contains no provider, server, Ghost, or
tool execution imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TaskKind = Literal["chat", "project", "research", "review", "planning"]


@dataclass(frozen=True)
class TaskSubmission:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    intent: str = "auto"
    run_id: str = ""
    # Model-sourced execution hint (e.g. the unified-auto PLAN). Never user
    # text: executors may read it via execution_task(), while persistence
    # (ledger excerpts, observations user_text, snapshots latest_user) always
    # reads task so model output can never wear the user's voice.
    model_hint: str = ""
    # User-authorized capabilities for this submission (e.g. ("web.read",)).
    # Recorded at the entry boundary from explicit user intent (task text,
    # Research button, UI toggles). Never derived from model_hint.
    requested_capabilities: tuple[str, ...] = ()
    # Strict Research was explicitly enabled for this submission.
    strict_research: bool = False
    # Explicit completion requirement from the task entry. The completion
    # gate never infers this from keywords or write permission; read-only
    # tasks keep False even when the policy still grants project.write.
    project_changes_required: bool = False
    sources_open_required: bool = False
    # Entry-generated explicit denials (e.g.明确只读 → project.write/shell.approval).
    # build_task_policy() 统一扣除，model_hint 与 requested 永远不能加回。
    denied_capabilities: tuple[str, ...] = ()
    # Shell approval continuation: previous run id (原策略复用）与已裁决的
    # shell 结果（turn-0 初始行，首发回答原 native 调用）。
    previous_run_id: str = ""
    initial_shell_results: tuple[dict[str, object], ...] = ()
    # Explicit review reuse source: prior completed run id whose structured
    # review may be reused when every identity and scope condition matches.
    # Empty means fresh review; never a file path.
    review_source_run_id: str = ""
    model_selection: dict[str, object] = field(default_factory=dict)


def derive_project_changes_required(
    body: dict[str, Any] | None,
    *,
    intent: str = "",
    project: str | None = None,
) -> bool:
    """Explicit entry decision: does this task require project modification?

    Write permission (can) never implies must. Only project/hybrid kinds
    with an attached project default to must-change; every other intent
    (chat/research/planning/readonly/review/auto) defaults to False.
    An explicit boolean ``project_changes_required`` in the submission body
    always wins, so a read-only project task can stay completable without
    edits. Never keyword-guesses from task text.
    """
    try:
        data = body if isinstance(body, dict) else dict[str, object]()
        if isinstance(data, dict) and "project_changes_required" in data:
            raw = data.get("project_changes_required")
            if raw is True:
                return True
            if raw is False:
                return False
    except Exception:
        pass
    try:
        kind = str(intent or "").strip().lower()
    except Exception:
        kind = ""
    try:
        has_project = bool(str(project or "").strip())
    except Exception:
        has_project = False
    if not has_project:
        return False
    # Only project and hybrid kinds contract to change files. Read-only
    # intents stay False even with a project attached.
    return kind in {"project", "hybrid"}


def execution_task(request: TaskSubmission) -> str:
    """Executor-visible prompt: user task plus the model hint, if any."""
    hint = str(getattr(request, "model_hint", "") or "").strip()
    if not hint:
        return request.task
    return f"{request.task}\n\nAuto plan:\n{hint}"
