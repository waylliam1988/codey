"""Shared path normalization for collected change payloads."""

from __future__ import annotations

from pathlib import PurePosixPath


def safe_change_path(value: object) -> str:
    """Return a normalized relative POSIX path, or ``""`` when unsafe."""
    import re as _re

    raw = str(value or "").strip()
    if not raw:
        return ""
    # Reject on the raw value before any slash stripping: a leading "/"
    # is a POSIX absolute path and "C:/" / "C:\\" / "\\\\" are Windows
    # absolute paths. Stripping first would launder them to relative.
    unified = raw.replace("\\", "/")
    if unified.startswith("/") or _re.match(r"^[A-Za-z]:", unified):
        return ""
    normalized = unified.strip().strip("/")
    if not normalized or normalized == ".":
        return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or path.as_posix() == ".":
        return ""
    return path.as_posix()


def change_file_paths(
    raw_path: object,
    raw_previous_path: object = "",
    raw_status: object = "",
) -> tuple[str, str]:
    """Normalize one collected change file row.

    ``git status --short`` renders renames/copies as ``old -> new``. The
    changed-file identity is the new path, while ``previous_path`` keeps the
    old path for review anchors and summaries.
    """

    previous_path = safe_change_path(raw_previous_path)
    path_text = str(raw_path or "")
    status = str(raw_status or "").strip().upper()
    if " -> " not in path_text or (
        not previous_path and not status.startswith(("R", "C"))
    ):
        return safe_change_path(path_text), previous_path
    before, after = path_text.split(" -> ", 1)
    path = safe_change_path(after)
    if not path:
        return safe_change_path(path_text), previous_path
    return path, previous_path or safe_change_path(before)


__all__ = ["change_file_paths", "safe_change_path"]
