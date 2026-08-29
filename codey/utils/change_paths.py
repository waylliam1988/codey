"""Shared path normalization for collected change payloads."""

from __future__ import annotations

from pathlib import PurePosixPath


def safe_change_path(value: object) -> str:
    """Return a normalized relative POSIX path, or ``""`` when unsafe."""

    normalized = str(value or "").replace("\\", "/").strip().strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
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
