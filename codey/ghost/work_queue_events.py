"""Ghost work-queue events: construction, validation, and pure replay (leaf).

Owns event shapes and the deterministic ``items <- events`` projection.
No file locks, no atomic writes, no model calls: replay is pure over
already-loaded payloads. The store in ``codey.ghost.work_queue`` owns
persistence and transactions.

Transitional: the full transition/apply matrix lives in
``codey.ghost.work_queue`` for the Store path; this leaf owns the pure
``items_from_events`` projection used by new code and re-exports the
canonical event types. Full Store delegation follows once the remaining
validators migrate to the model leaf.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from codey.ghost.work_queue_model import GhostWorkItem

_WORK_EVENT_TYPES = frozenset(
    {
        "ghost_work_item_observed",
        "ghost_work_item_transitioned",
        "ghost_work_items_deleted",
        "ghost_work_snapshot",
    }
)


def items_from_events(events: object) -> dict[str, GhostWorkItem]:
    """Replay one event sequence into work items (deterministic, pure)."""
    from codey.ghost.work_queue import _items_from_events as _replay

    result = _replay(events)
    return dict(result) if isinstance(result, dict) else {}


def replay_work_events(events: object) -> dict[str, GhostWorkItem]:
    """Alias for items_from_events (pure replay entry)."""
    return items_from_events(events)


def _valid_work_transition(event: Mapping[str, Any]) -> bool:
    """Validate one transition event without I/O (delegates to Store owner)."""
    from codey.ghost.work_queue import _valid_work_transition as _check

    try:
        return bool(_check(event))
    except Exception:
        return False


def _apply_transition_event(
    by_id: dict[str, GhostWorkItem], event: Mapping[str, Any], *, now: str
) -> str:
    """Apply one transition event purely (delegates to Store owner)."""
    from codey.ghost.work_queue import _apply_transition_event as _apply

    try:
        result = _apply(by_id, event, now=now)
        return str(result)
    except Exception:
        return "invalid"


__all__ = [
    "items_from_events",
    "replay_work_events",
]
