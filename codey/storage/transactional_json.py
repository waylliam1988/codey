"""Locked, atomic JSON mutations for local Codey state.

An atomic write is not a transaction: ``temp + os.replace`` guarantees that
readers never see a torn file, but two concurrent processes can both read
version N and both write their own N+1 -- silently dropping one side's
mutation. These primitives wrap durable writes in advisory file locks
so read-modify-write cycles against one state file are serialized.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from codey.storage.file_lock import (
    LOCK_POLL_INTERVAL,
    LOCK_TIMEOUT_SECONDS,
    LockTimeout,
    reset_event_backed_state,
    with_file_lock,
)
from codey.storage.local_store import MAX_JSON_BYTES, read_json, write_json_atomic


def mutate_json_atomic(
    path: str | Path,
    mutator: Callable[[dict | None], dict | None],
    *,
    max_bytes: int = MAX_JSON_BYTES,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> dict | None:
    """Locked read-modify-write of one JSON object file.

    ``mutator`` receives the parsed current object (``None`` when absent or
    unreadable) and returns the next object to persist; returning ``None``
    aborts without writing. Returns the payload now stored at ``path``.
    """

    target = Path(path)
    with with_file_lock(target, timeout_seconds=timeout_seconds):
        current = read_json(target, max_bytes=max_bytes)
        updated = mutator(current)
        if updated is None:
            return current
        write_json_atomic(target, updated, max_bytes=max_bytes)
        return updated


def append_json_array_locked(
    path: str | Path,
    rows: list[dict],
    *,
    max_bytes: int = MAX_JSON_BYTES,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> list[dict]:
    """Locked append of rows to a JSON array file, creating it when absent.

    Raises ValueError when the resulting array would exceed ``max_bytes``;
    the previous contents stay intact because the write itself is atomic.
    """

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TypeError("append rows must be a list of objects")
    target = Path(path)

    def _mutate(current: dict | None) -> dict | None:
        raw_items = current.get("items") if isinstance(current, dict) else None
        merged = [
            *(item for item in (raw_items or []) if isinstance(item, dict)),
            *rows,
        ]
        return {"schema_version": 1, "items": merged}

    payload = mutate_json_atomic(
        target,
        _mutate,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    )
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


__all__ = [
    "LockTimeout",
    "LOCK_POLL_INTERVAL",
    "LOCK_TIMEOUT_SECONDS",
    "append_json_array_locked",
    "mutate_json_atomic",
    "reset_event_backed_state",
    "with_file_lock",
]
