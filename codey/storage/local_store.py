"""Small atomic JSON storage for Codey's local runtime state."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path


DEFAULT_STATE_HOME = Path.home() / ".codey"
MAX_JSON_BYTES = 8 * 1024 * 1024


def project_key(project: str | Path) -> str:
    resolved = os.path.normcase(str(Path(project).expanduser().resolve()))
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def session_key(session_id: str) -> str:
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:24]


def read_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict | None:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json_atomic(
    path: Path,
    value: dict,
    *,
    max_bytes: int = MAX_JSON_BYTES,
) -> None:
    data = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError("local state is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def delete_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
