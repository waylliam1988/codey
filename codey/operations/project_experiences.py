"""Revalidate the evaluated project history treatment without model requests."""

from __future__ import annotations

import sqlite3

from codey.agents.request import AgentRequest
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    render_retrieved_block,
    retrieve_relevant_observations,
)
from codey.ghost.observations import GhostObservationStore


def validated_project_preferences(request: AgentRequest) -> str:
    """Read user/project/session preferences for the actual project consumer."""
    if not request.admit_hints or request.workspace_revision_store is None:
        return ""
    try:
        home = request.workspace_revision_store.state_home
        inbox = GhostInboxStore(home)
        if not all(inbox.control_enabled(control, project=str(request.project), session_id=request.session_id)
                   for control in ("retrieve_projections", "admit_hints")):
            return ""
        return build_ghost_directive(
            GhostHebbianStore(home), project=str(request.project), session_id=request.session_id,
            affinity_store=GhostAffinityStore(home),
        ).text
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return ""


def validated_project_experiences(request: AgentRequest) -> str:
    """Return current, in-scope history independently of generation settings.

    Re-reading committed rows checks deletion, consumption windows and durable
    input authority. Lexical ranking never calls an embedding service.
    """
    if not request.admit_hints or not request.ghost_experiences:
        return ""
    if request.permission_profile != "coding_writer" or request.workspace_revision_store is None:
        return ""
    try:
        home = request.workspace_revision_store.state_home
        inbox = GhostInboxStore(home)
        for control in ("retrieve_projections", "admit_hints"):
            if not inbox.control_enabled(control, project=str(request.project), session_id=request.session_id):
                return ""
        store = GhostObservationStore(home)
        rows = tuple(row for row in store.read_committed(
            project=str(request.project), session_id=request.session_id, scope="project",
        ) if row["run_id"] != request.run_id)
        picked = retrieve_relevant_observations(
            rows, request.task, max_items=MAX_RETRIEVED_ITEMS, budget_chars=RETRIEVAL_BUDGET_CHARS,
        )
        if not picked:
            return ""
        current = render_retrieved_block(picked, RETRIEVAL_BUDGET_CHARS)
        return current if current == request.ghost_experiences else ""
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return ""


__all__ = ["validated_project_experiences", "validated_project_preferences"]
