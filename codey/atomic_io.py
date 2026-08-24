"""Atomic user-file writes.

User source code must survive a crash at least as well as Codey's own state
files do: write to a temp file in the same directory, fsync, then
``os.replace``. The original file is untouched until the replace succeeds,
and the file's existing CRLF/LF style is preserved instead of being rewritten
by platform text-mode translation.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


def encode_with_original_eol(target: Path, text: str, *, encoding: str = "utf-8") -> bytes:
    """Encode ``text`` matching the target file's recorded newline style."""

    try:
        raw = target.read_bytes()
    except (OSError, ValueError):
        raw = b""
    if b"\r\n" in raw:
        normalized = str(text or "").replace("\r\n", "\n")
        return normalized.replace("\n", "\r\n").encode(encoding)
    return str(text or "").replace("\r\n", "\n").encode(encoding)


def write_text_atomic(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` atomically, preserving EOL style."""

    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    data = encode_with_original_eol(target, text, encoding=encoding)
    tmp = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    existing_mode = _existing_mode(target)
    try:
        with tmp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        os.replace(tmp, target)
    finally:
        _cleanup_temp_file(tmp)


def _existing_mode(target: Path) -> int | None:
    try:
        return stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return None


def _cleanup_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            os.chmod(path, mode | stat.S_IWRITE | stat.S_IWUSR)
            path.unlink(missing_ok=True)
        except OSError:
            pass
    except OSError:
        pass


__all__ = [
    "encode_with_original_eol",
    "write_text_atomic",
]
