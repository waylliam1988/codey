"""Small atomic JSON storage for Codey's local runtime state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


DEFAULT_STATE_HOME = Path.home() / ".codey"
MAX_JSON_BYTES = 8 * 1024 * 1024


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
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]


def read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict | None:
    """Lenient cache read: missing or corrupt state -> None.

    Cache-like stores (ghost learning, facts, conversations) use this: a
    corrupt cache resets to empty, matching historical behavior. Recovery-
    critical state (snapshot baselines) must use read_json_strict below so
    corruption is backed up instead of silently reset.
    """
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_json_strict(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict | None:
    """Strict read distinguishing missing (None) from corrupt (raise).

    Missing file -> None. Oversize, undecodable, or non-dict ->
    StoreCorruption. Recovery-critical callers catch it, back up the corrupt
    file, and only then reset.
    """
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            raise StoreCorruption(path, "too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except StoreCorruption:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreCorruption(path, type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise StoreCorruption(path, "not a dict")
    return value


def write_json_atomic(
    path: Path,
    value: dict,
    *,
    mode: int | None = None,
    preserve_mode: bool = True,
    max_bytes: int = MAX_JSON_BYTES,
) -> None:
    from codey.storage.atomic_io import write_json_atomic as _atomic_write_json

    _atomic_write_json(
        path,
        value,
        mode=mode,
        preserve_mode=preserve_mode,
        max_bytes=max_bytes,
    )


def delete_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def backup_corrupt_file(path: Path) -> Path | None:
    """Rename a corrupt state file aside for forensics.

    Returns the backup path, or None when there is nothing to back up.
    Every strict reader calls this before resetting to empty so corruption
    is observable instead of silent.
    """
    try:
        target = Path(path)
        if not target.is_file():
            return None
        backup = target.with_name(target.name + ".corrupt")
        target.replace(backup)
        return backup
    except OSError:
        return None
