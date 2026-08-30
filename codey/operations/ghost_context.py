"""Prompt-time Ghost context projections.

These helpers are read-side context builders. They do not learn from the turn,
claim queued work, or mutate Ghost stores.
"""

from __future__ import annotations

from typing import Any

from codey.ghost.continuity import build_ghost_continuity
from codey.ghost.directive import build_ghost_directive


def ghost_affinity_store(state: Any):
    store = getattr(state, "ghost_affinity", None)
    if store is None:
        return None
    inbox_store = getattr(state, "ghost_inbox", None)
    if inbox_store is not None:
        try:
            if not inbox_store.learning_enabled():
                return None
        except Exception:
            return None
    return store


def ghost_directive(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
):
    store = getattr(state, "ghost_hebbian", None)
    if store is None:
        return build_ghost_directive(None)
    try:
        return build_ghost_directive(
            store,
            project=project,
            session_id=session_id,
            affinity_store=ghost_affinity_store(state),
        )
    except Exception:
        return build_ghost_directive(None)


def ghost_directive_text(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
) -> str:
    return ghost_directive(state, project=project, session_id=session_id).text


def ghost_continuity(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
):
    store = getattr(state, "ghost_continuity", None)
    if store is None:
        return build_ghost_continuity(None)
    try:
        return build_ghost_continuity(
            store,
            project=project,
            session_id=session_id,
        )
    except Exception:
        return build_ghost_continuity(None)


def ghost_continuity_text(
    state: Any,
    *,
    project: str = "",
    session_id: str = "",
) -> str:
    return ghost_continuity(state, project=project, session_id=session_id).text


__all__ = [
    "ghost_affinity_store",
    "ghost_continuity",
    "ghost_continuity_text",
    "ghost_directive",
    "ghost_directive_text",
]
