"""Provider-independent confirmed project goals for normal task consumers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from codey.agents.request import AgentRequest
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.project_goals import GOAL_BUDGET, HISTORY_BUDGET, confirmed_project_goal_text
from codey.workspace.context_admission import GhostContextSource, request_scope
from codey.workspace.context_source import ContextSource, render_context_source


def validated_project_goal_sources(request: AgentRequest) -> tuple[GhostContextSource, ...]:
    from codey.policies.permissions import allows_context_source, profile_for_name

    if (not request.admit_hints
            or request.workspace_revision_store is None):
        return ()
    profile = profile_for_name(request.permission_profile)
    if not all(allows_context_source(profile, key) for key in
               ("ghost_project_goal_history", "ghost_confirmed_project_goal")):
        return ()
    return project_goal_sources(request.workspace_revision_store.state_home, project=str(request.project),
                                session_id=request.session_id, task=request.task, exclude_run_id=request.run_id)


def project_goal_sources(home: Path, *, project: str, session_id: str, task: str,
                         exclude_run_id: str) -> tuple[GhostContextSource, ...]:
    """Re-read grounded goals; the old storage family retains its context."""
    if not project:
        return ()
    try:
        inbox = GhostInboxStore(home)
        observations = GhostObservationStore(home)
        with inbox.database.write_transaction():
            if not all(inbox.control_enabled(control, project=project, session_id=session_id)
                       for control in ("retrieve_projections", "admit_hints")):
                return ()
            history, goal = confirmed_project_goal_text(
                inbox, observations, project=project, exclude_run_id=exclude_run_id,
                include_storage=any(term in task.casefold() for term in ("storage", "存储", "儲存")),
            )
        if not history or not goal:
            return ()
        scope = request_scope(project, session_id)
        sources: list[GhostContextSource] = []
        for key, value, budget in (("ghost_project_goal_history", history, HISTORY_BUDGET),
                                   ("ghost_confirmed_project_goal", goal, GOAL_BUDGET)):
            def load_value(text: str = value) -> str:
                return text

            source = render_context_source(ContextSource(key, load_value, budget, "committed_goal_snapshot",
                                                         "manual_project_goal", scope=scope))
            if source is None or source.truncated:
                return ()
            sources.append(GhostContextSource(key, source.text, budget, scope))
        return tuple(sources)
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return ()
