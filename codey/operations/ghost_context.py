"""Prompt-time Ghost context projections.

These helpers are read-side context builders. They do not learn from the turn,
claim queued work, or mutate Ghost stores. No helper here calls a model.

Legacy data note: ``ghost_directive()`` still reads the existing hebbian store
(plus affinity) and ``ghost_continuity()`` still reads the continuity store.
Automatic structured writes into inbox/hebbian stopped with the
experience-memory final state, but previously confirmed rows keep
participating in prompts through these two builders, rendered under their own
established labels. New experience observations are retrieved separately via
``ghost_experiences()`` and rendered under an explicit history label, never
presented as confirmed long-term traits.
"""

from __future__ import annotations

from typing import Any

from codey.ghost.continuity import build_ghost_continuity
from codey.ghost.directive import build_ghost_directive
from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    render_retrieved_block,
    retrieve_relevant_observations,
)


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


def ghost_experiences(
    state: Any,
    *,
    session_id: str = "",
    project: str = "",
    query: str = "",
    exclude_run_id: str = "",
    max_items: int = MAX_RETRIEVED_ITEMS,
    budget_chars: int = RETRIEVAL_BUDGET_CHARS,
) -> str:
    """Retrieve committed experience observations for the next normal call.

    Zero model calls. Returns a self-labeled history block or "". Retrieval is
    gated on Ghost updates being enabled; callers prepend/append it to their
    existing prompt alongside directive/continuity. Only ``committed``
    (successfully finished) rounds are returned; the current run is excluded.
    """
    try:
        inbox = getattr(state, "ghost_inbox", None)
        if inbox is not None and not bool(inbox.learning_enabled()):
            return ""
    except Exception:
        pass
    try:
        store = getattr(state, "ghost_observations", None)
        if store is None:
            return ""
        rows = store.read_committed(session_id=session_id, project=project or "")
    except Exception:
        return ""
    try:
        picked = retrieve_relevant_observations(
            rows,
            str(query or ""),
            exclude_run_id=str(exclude_run_id or ""),
            max_items=max_items,
            budget_chars=budget_chars,
        )
    except Exception:
        return ""
    if not picked:
        return ""
    return render_retrieved_block(picked)


__all__ = [
    "ghost_affinity_store",
    "ghost_continuity",
    "ghost_directive",
    "ghost_experiences",
]
