"""Ghost work-queue sources: continuity/research/checkpoint conversions (leaf).

Owns multi-source work-item generation from already-loaded projections.
No file locks or persistence: the store owns transactions.

Transitional: the full source converters live in
``codey.ghost.work_queue`` for the Store path; this leaf owns the stable
``items_from_continuity`` entry used by new code. Full Store delegation
follows once the remaining converters migrate here.
"""

from __future__ import annotations

from typing import Any

from codey.ghost.work_queue_model import GhostWorkItem


def items_from_continuity(*args: Any, **kwargs: Any) -> Any:
    """Convert continuity projection into work items (stable ids)."""
    from codey.ghost.work_queue import _items_from_continuity as _convert

    return _convert(*args, **kwargs)


def _new_item(*args: Any, **kwargs: Any) -> GhostWorkItem:
    """Create one work item (delegates to Store owner until migration)."""
    from codey.ghost.work_queue import _new_item as _create

    return _create(*args, **kwargs)


__all__ = [
    "items_from_continuity",
]
