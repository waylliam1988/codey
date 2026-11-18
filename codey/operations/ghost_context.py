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
from typing import Protocol, runtime_checkable

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuity, GhostContinuityStore, build_ghost_continuity
from codey.ghost.directive import GhostDirective, build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostSettingsError
from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    HistoryView,
    render_retrieved_block,
)
from codey.ghost.observations import GhostObservationCorruptedError, GhostObservationStore


@runtime_checkable
class _WarningSink(Protocol):
    def emit(self, event: dict[str, object]) -> object: ...


@runtime_checkable
class _ControlReader(Protocol):
    def control_enabled(self, name: str, *, project: str = "", session_id: str = "") -> bool: ...


def ghost_control_enabled(
    state: object,
    name: str,
    *,
    project: str = "",
    session_id: str = "",
) -> bool:
    """Read an independent Ghost control and fail closed on bad settings."""
    inbox: object = getattr(state, "ghost_inbox", None)
    if not isinstance(inbox, _ControlReader):
        return False
    try:
        value = inbox.control_enabled(name, project=project, session_id=session_id)
        return value if type(value) is bool else False
    except (GhostSettingsError, OSError, ValueError, TypeError, AttributeError):
        return False


def ghost_affinity_store(
    state: object,
    *,
    project: str = "",
    session_id: str = "",
) -> GhostAffinityStore | None:
    store: object = getattr(state, "ghost_affinity", None)
    if not isinstance(store, GhostAffinityStore):
        return None
    if not ghost_control_enabled(state, "accept_events", project=project, session_id=session_id):
        return None
    return store


def ghost_directive(
    state: object,
    *,
    project: str = "",
    session_id: str = "",
    provider: object = None,
) -> GhostDirective:
    if not ghost_control_enabled(state, "retrieve_projections", project=project, session_id=session_id):
        return build_ghost_directive(None)
    store: object = getattr(state, "ghost_hebbian", None)
    if not isinstance(store, GhostHebbianStore):
        return build_ghost_directive(None)
    return build_ghost_directive(
        store,
        project=project,
        session_id=session_id,
        affinity_store=ghost_affinity_store(state, project=project, session_id=session_id),
    )


def ghost_continuity(
    state: object,
    *,
    project: str = "",
    session_id: str = "",
) -> GhostContinuity:
    if not ghost_control_enabled(state, "retrieve_projections", project=project, session_id=session_id):
        return build_ghost_continuity(None)
    store: object = getattr(state, "ghost_continuity", None)
    if not isinstance(store, GhostContinuityStore):
        return build_ghost_continuity(None)
    return build_ghost_continuity(
        store,
        project=project,
        session_id=session_id,
    )


def ghost_hints_enabled(
    state: object,
    *,
    project: str = "",
    session_id: str = "",
    provider: object = None,
    directive_text: str = "",
) -> bool:
    """Recheck every preference source at each physical send attempt."""
    if not ghost_control_enabled(state, "admit_hints", project=project, session_id=session_id):
        return False
    if directive_text:
        current = ghost_directive(state, project=project, session_id=session_id, provider=provider)
        return current.text == directive_text
    return True


def ghost_experiences(
    state: object,
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
    view: HistoryView = "dialogue" if scope == "project" else "requests"
    try:
        store: object = getattr(state, "ghost_observations", None)
        if not isinstance(store, GhostObservationStore):
            return ""
        picked = store.retrieve(
            query or "", session_id=session_id, project=project or "", scope=scope,
            exclude_run_id=(exclude_run_id or ""),
            max_items=max_items,
            budget_chars=budget_chars, view=view,
        )
    except (GhostObservationCorruptedError, OSError, TypeError, ValueError, OverflowError, sqlite3.Error, ImportError):
        return ""
    if not picked:
        return ""
    return render_retrieved_block(picked, budget_chars, view=view)


def _emit_settings_warning(state: object, *, session_id: str = "") -> None:
    """Surface a corrupt learning switch as a visible local-health warning."""
    if not isinstance(state, _WarningSink):
        return
    try:
        state.emit({
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
    "ghost_hints_enabled",
    "ghost_experiences",
]
