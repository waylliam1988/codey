"""Task-facing data model.

This module is the boundary between user-visible task submission protocols and
the internal runtime.  It deliberately contains no provider, server, Ghost, or
tool execution imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


def execution_task(request: TaskSubmission) -> str:
    """Executor-visible prompt: user task plus the model hint, if any."""
    hint = str(getattr(request, "model_hint", "") or "").strip()
    if not hint:
        return request.task
    return f"{request.task}\n\nAuto plan:\n{hint}"
