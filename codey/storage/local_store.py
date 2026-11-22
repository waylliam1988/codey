"""Small atomic JSON storage for Codey's local runtime state."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

from codey.storage.atomic_io import MAX_ATOMIC_JSON_BYTES
from codey.storage.atomic_io import write_json_atomic as write_json_atomic
from codey.utils.object_fields import object_fields

DEFAULT_STATE_HOME = Path.home() / ".codey"
MAX_JSON_BYTES = MAX_ATOMIC_JSON_BYTES


class StoreCorruption(ValueError):
    """Local JSON state exists but cannot be trusted (corrupt/oversize)."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"corrupt local state {path}: {reason}")
        self.path = Path(path)
        self.reason = reason


def project_key(project: str | Path) -> str:
    resolved = os.path.normcase(str(Path(project).expanduser().resolve()))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, object] | None:
    """Lenient cache read: missing or corrupt state -> None.

    Cache-like stores (ghost learning, facts, conversations) use this: a
    corrupt cache resets to empty, matching historical behavior. Recovery-
    critical state (snapshot baselines) must use read_json_strict below so
    corruption is backed up instead of silently reset.
    """
    try:
        return read_json_strict(path, max_bytes=max_bytes)
    except StoreCorruption:
        return None


def read_json_strict(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, object] | None:
    """Strict read distinguishing missing (None) from corrupt (raise).

    Missing file -> None. Oversize, undecodable, or non-dict ->
    StoreCorruption. Recovery-critical callers catch it, back up the corrupt
    file, and only then reset.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(max(0, max_bytes) + 1)
        if len(data) > max_bytes:
            raise StoreCorruption(path, "too large")
        value: object = json.loads(data.decode("utf-8"))
    except FileNotFoundError:
        return None
    except StoreCorruption:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise StoreCorruption(path, type(exc).__name__) from exc
    if not object_fields(value):
        raise StoreCorruption(path, "not a dict")
    return value


def delete_file(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def backup_corrupt_file(path: Path) -> Path | None:
    """Rename a corrupt state file aside for forensics, never overwriting.

    The first backup is ``<name>.corrupt``; later ones append a counter
    (``.corrupt.1``, ``.corrupt.2``, …) so repeated corruption never destroys
    an earlier backup. Returns the backup path, or None when there is
    nothing to back up.
    """
    try:
        target = Path(path)
        if not target.is_file():
            return None
        backup = target.with_name(target.name + ".corrupt")
        index = 0
        while backup.exists():
            index += 1
            if index > 999:
                return None
            backup = target.with_name(f"{target.name}.corrupt.{index}")
        target.replace(backup)
        return backup
    except OSError:
        return None
