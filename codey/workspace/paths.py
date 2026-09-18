"""Shared workspace path and bounded file helpers."""

from __future__ import annotations

from collections.abc import Callable
import errno
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath


def safe_join(root: str | Path, rel: str, *, label: str = "project root") -> Path:
    """Resolve ``rel`` under ``root`` and prevent relative path traversal.

    Threat model: application-level guard against model-generated escapes
    (``../../etc/passwd``); not an OS-level capability sandbox (symlink
    races / TOCTOU across directory swaps need the no-follow helpers).
    """
    resolved_root = Path(root).expanduser().resolve()
    path = (resolved_root / str(rel)).resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise ValueError(f"path escapes {label}: {rel}")
    return path


def ensure_not_symlink(path: str | Path) -> Path:
    """Fail closed when the final component is a symlink.

    Windows has no full O_NOFOLLOW, so every no-follow reader/writer calls
    this immediately before open: the lstat-then-open window stays, but a
    swapped-in symlink is rejected instead of followed.
    """
    target = Path(path)
    try:
        if target.is_symlink():
            raise ValueError(f"refusing symlink: {target}")
    except OSError as exc:
        raise ValueError(f"not a file: {target}") from exc
    return target


def read_text_bounded_no_follow(path: str | Path, *, max_bytes: int) -> str:
    """Read a bounded UTF-8 file without following a trailing symlink."""
    target = ensure_not_symlink(path)
    limit = max(0, int(max_bytes))
    try:
        info = target.lstat()
    except OSError as exc:
        raise ValueError(f"not a file: {target}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"refusing symlink: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a file: {target}")
    if info.st_size > limit:
        raise ValueError(f"file too large for snapshot: {target}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise ValueError(f"not a file: {target}") from exc
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"not a file: {target}") from exc
    if len(data) > limit:
        raise ValueError(f"file too large for snapshot: {target}")
    text = data.decode("utf-8")
    # Match Path.read_text universal-newline semantics so snapshot hashes are
    # stable across the old/new readers (raw CRLF on disk -> "\n" in memory).
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text_bounded(path: str | Path, *, max_bytes: int) -> str:
    return read_text_bounded_no_follow(path, max_bytes=max_bytes)


def read_text_or_none(path: str | Path, *, max_bytes: int) -> str | None:
    target = Path(path)
    if not target.exists():
        return None
    return read_text_bounded(target, max_bytes=max_bytes)


def content_hash(content: str | None) -> str:
    if content is None:
        return "missing"
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def path_hash(path: str | Path) -> str:
    """Hash a text file without following a trailing symlink.

    Opens with ``O_NOFOLLOW`` (POSIX) so a swapped-in link fails with
    ``ELOOP`` instead of hashing another file's content; the ``lstat`` pre-check covers
    Windows, where ``O_NOFOLLOW`` is unavailable. Reads through the fd in
    bounded text chunks with universal-newline semantics, matching
    :func:`content_hash`.
    """
    target = Path(path)
    try:
        info = target.lstat()
    except OSError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"refusing symlink: {target}")
    if not stat.S_ISREG(info.st_mode):
        try:
            if not target.exists():
                return "missing"
        except OSError:
            return "missing"
        raise ValueError(f"not a file: {target}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"refusing symlink: {target}") from exc
        if isinstance(exc, FileNotFoundError):
            return "missing"
        raise ValueError(f"not a file: {target}") from exc
    try:
        with os.fdopen(fd, "r", encoding="utf-8", newline=None) as handle:
            try:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError(f"not a file: {target}")
            except OSError as exc:
                raise ValueError(f"not a file: {target}") from exc
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), ""):
                digest.update(chunk.encode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise
    except OSError as exc:
        raise ValueError(f"not a file: {target}") from exc
    return "sha256:" + digest.hexdigest()


def bounded_directory_entries(
    path: Path,
    max_entries: int,
    *,
    include_hidden: bool = True,
    sort_key: Callable[[Path], object] | None = None,
    check_cancel: Callable[[], None] | None = None,
    swallow_errors: bool = False,
) -> tuple[list[Path], bool]:
    if max_entries <= 0:
        return [], True
    entries: list[Path] = []
    try:
        for index, entry in enumerate(path.iterdir()):
            if check_cancel is not None:
                check_cancel()
            if index >= max_entries:
                return _sorted_entries(entries, sort_key), True
            if include_hidden or not entry.name.startswith("."):
                entries.append(entry)
    except OSError:
        if swallow_errors:
            return [], False
        raise
    return _sorted_entries(entries, sort_key), False


def is_test_path(rel: str) -> bool:
    path = PurePosixPath(str(rel or "").replace("\\", "/"))
    lower_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    return (
        any(part in {"test", "tests", "__tests__"} for part in lower_parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _sorted_entries(
    entries: list[Path],
    sort_key: Callable[[Path], object] | None,
) -> list[Path]:
    if sort_key is None:
        return sorted(entries)
    return sorted(entries, key=sort_key)


__all__ = [
    "bounded_directory_entries",
    "content_hash",
    "ensure_not_symlink",
    "is_test_path",
    "path_hash",
    "read_text_bounded",
    "read_text_bounded_no_follow",
    "read_text_or_none",
    "safe_join",
]
