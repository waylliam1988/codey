"""Prompt-time Ghost context projections.

These helpers are read-side context builders. They do not learn from the turn,
claim queued work, or mutate Ghost stores. No helper here calls a model.

``ghost_directive()`` reads reviewed preferences from Hebbian memory (plus
affinity), including preferences confirmed through the local context panel.
``ghost_continuity()`` reads the continuity store. Experience observations are
retrieved separately by ``ghost_experiences()`` and rendered as history, never
presented as confirmed long-term traits. These builders make no model calls.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from codey.ghost.continuity import build_ghost_continuity
from codey.ghost.directive import build_ghost_directive
from codey.ghost.inbox import GhostSettingsError
from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    render_retrieved_block,
)
from codey.ghost.observations import GhostObservationCorruptedError


def ghost_control_enabled(
    state: Any,
    name: str,
    *,
    project: str = "",
    session_id: str = "",
) -> bool:
    """Read an independent Ghost control and fail closed on bad settings."""
    inbox = getattr(state, "ghost_inbox", None)
    if inbox is None:
        return False
    try:
        value = inbox.control_enabled(name, project=project, session_id=session_id)
        return value if type(value) is bool else False
    except (GhostSettingsError, OSError, ValueError, TypeError, AttributeError):
        return False


def ghost_affinity_store(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
) -> Any:
    store = getattr(state, "ghost_affinity", None)
    if store is None:
        return None
    if not ghost_control_enabled(state, "accept_events", project=project, session_id=session_id):
        return None
    return store


def ghost_directive(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
) -> Any:
    if not ghost_control_enabled(state, "retrieve_projections", project=project, session_id=session_id):
        return build_ghost_directive(None)
    store = getattr(state, "ghost_hebbian", None)
    if store is None:
        return build_ghost_directive(None)
    return build_ghost_directive(
        store,
        project=project,
        session_id=session_id,
        affinity_store=ghost_affinity_store(state, project=project, session_id=session_id),
    )


def ghost_continuity(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
) -> Any:
    if not ghost_control_enabled(state, "retrieve_projections", project=project, session_id=session_id):
        return build_ghost_continuity(None)
    store = getattr(state, "ghost_continuity", None)
    if store is None:
        return build_ghost_continuity(None)
    return build_ghost_continuity(
        store,
        project=project,
        session_id=session_id,
    )


def ghost_experiences(
    state: Any,
    *,
    session_id: str = "",
    project: str = "",
    query: str = "",
    exclude_run_id: str = "",
    max_items: int = MAX_RETRIEVED_ITEMS,
    budget_chars: int = RETRIEVAL_BUDGET_CHARS,
    scope: str = "session",
) -> str:
    """Retrieve committed experience observations for the next normal call.

    Returns a self-labeled history block or "". The explicitly selected
    semantic mode calls its embedding endpoint; lexical mode is local. Retrieval is
    gated on Ghost updates being enabled; callers prepend/append it to their
    existing prompt alongside directive/continuity. Only ``committed``
    (successfully finished) rounds are returned; the current run is excluded.
    ``scope`` follows the store's ownership scopes (session/project/user);
    project writers pass ``project`` scope for cross-session recall.

    A corrupt learning switch fails closed: no retrieval and a visible
    ``ghost_post_turn_warning`` so the UI shows "Local update paused"
    instead of silently serving stale observations.
    """
    try:
        if not ghost_control_enabled(state, "retrieve_projections", project=project, session_id=session_id):
            return ""
    except (GhostSettingsError, OSError, ValueError, TypeError):
        _emit_settings_warning(state, session_id=session_id)
        return ""
    try:
        store = getattr(state, "ghost_observations", None)
        if store is None:
            return ""
        picked = store.retrieve(
            query or "", session_id=session_id, project=project or "", scope=scope,
            exclude_run_id=(exclude_run_id or ""),
            max_items=max_items,
            budget_chars=budget_chars,
        )
    except (GhostObservationCorruptedError, OSError, TypeError, ValueError, OverflowError, sqlite3.Error, ImportError):
        return ""
    if not picked:
        return ""
    return render_retrieved_block(picked, budget_chars)


def _emit_settings_warning(state: Any, *, session_id: str = "") -> None:
    """Surface a corrupt learning switch as a visible local-health warning."""
    emit = getattr(state, "emit", None)
    if not callable(emit):
        return
    try:
        emit({
            "type": "ghost_post_turn_warning",
            "stage": "learning_settings",
            "run_id": "",
            "session_id": (session_id or ""),
            "error_type": "GhostSettingsCorrupt",
            "error_ref": "ghost_settings_corrupt",
        })
    except Exception:
        return


__all__ = [
    "ghost_control_enabled",
    "ghost_affinity_store",
    "ghost_continuity",
    "ghost_directive",
    "ghost_experiences",
]
