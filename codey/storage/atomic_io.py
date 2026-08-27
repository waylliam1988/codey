"""Atomic user-file writes.

User source code must survive a crash at least as well as Codey's own state
files do: write to a temp file in the same directory, fsync, then
``os.replace``. The original file is untouched until the replace succeeds,
and the file's existing CRLF/LF style is preserved instead of being rewritten
by platform text-mode translation.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

MAX_ATOMIC_JSON_BYTES = 8 * 1024 * 1024


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


def write_bytes_atomic(
    path: str | Path,
    data: bytes,
    *,
    mode: int | None = None,
    preserve_mode: bool = True,
) -> None:
    """Replace ``path`` with ``data`` atomically."""

    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    existing_mode = _existing_mode(target)
    creation_mode = (
        mode if mode is not None else (existing_mode if (preserve_mode and existing_mode is not None) else 0o666)
    )
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(tmp, flags, creation_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            if mode is not None:
                _apply_mode(handle.fileno(), tmp, mode, required=True)
            elif preserve_mode and existing_mode is not None:
                _apply_mode(handle.fileno(), tmp, existing_mode, required=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_dir(directory)
    finally:
        _cleanup_temp_file(tmp)


def _apply_mode(fileno: int, path: Path, mode: int, *, required: bool = False) -> None:
    first_error: OSError | None = None
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fileno, mode)
            return
        except OSError as exc:
            first_error = exc
    try:
        os.chmod(path, mode)
    except OSError as exc:
        if required:
            if first_error is not None:
                raise exc from first_error
            raise


def _fsync_dir(directory: Path) -> None:
    if os.name == "posix":
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            dir_fd = os.open(str(directory), flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def write_text_atomic(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    preserve_mode: bool = True,
) -> None:
    """Replace ``path`` with ``text`` atomically, preserving EOL style."""

    target = Path(path)
    data = encode_with_original_eol(target, text, encoding=encoding)
    write_bytes_atomic(target, data, mode=mode, preserve_mode=preserve_mode)


def write_json_atomic(
    path: str | Path,
    value: dict,
    *,
    mode: int | None = None,
    preserve_mode: bool = True,
    max_bytes: int = MAX_ATOMIC_JSON_BYTES,
) -> None:
    """Replace ``path`` with JSON-serialized ``value`` atomically."""

    data = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError("local state is too large")
    write_bytes_atomic(path, data, mode=mode, preserve_mode=preserve_mode)


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
    "write_bytes_atomic",
    "write_json_atomic",
    "write_text_atomic",
]
