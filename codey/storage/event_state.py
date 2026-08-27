"""Safe event-backed state reset utilities."""

from __future__ import annotations

from pathlib import Path

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import delete_file


def reset_event_backed_state(
    events_path: str | Path,
    *state_paths: str | Path,
    timeout_seconds: float | None = None,
) -> None:
    """Safely reset event log and projection files under the event lock."""
    ep = Path(events_path)
    with with_file_lock(ep, timeout_seconds=timeout_seconds):
        for sp in state_paths:
            delete_file(Path(sp))
        delete_file(ep)


__all__ = [
    "reset_event_backed_state",
]
