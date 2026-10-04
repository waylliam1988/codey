"""Shared path normalization for collected change payloads."""

from __future__ import annotations

from pathlib import PurePosixPath


def decode_git_path(value: object) -> str:
    """Decode one Git C-style quoted path, preserving meaningful spaces."""
    if not isinstance(value, str):
        return ""
    raw = value
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        return _decode_c_quotes(inner)
    return raw.strip()


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

    previous_decoded = decode_git_path(raw_previous_path) if isinstance(raw_previous_path, str) else ""
    previous_path = safe_change_path(previous_decoded)
    path_text = str(raw_path or "")
    status = str(raw_status or "").strip().upper()
    if " -> " not in path_text or (
        not previous_path and not status.startswith(("R", "C"))
    ):
        return safe_change_path(decode_git_path(path_text)), previous_path
    before, after = path_text.split(" -> ", 1)
    path = safe_change_path(decode_git_path(after))
    if not path:
        return safe_change_path(decode_git_path(path_text)), previous_path
    return path, previous_path or safe_change_path(decode_git_path(before))


def _decode_c_quotes(inner: str) -> str:
    result = bytearray()
    index = 0
    length = len(inner)
    while index < length:
        char = inner[index]
        if char != "\\" or index + 1 >= length:
            result.extend(char.encode("utf-8"))
            index += 1
            continue
        nxt = inner[index + 1]
        if nxt in "01234567":
            octal = inner[index + 1 : index + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                result.append(int(octal, 8))
                index += 4
                continue
            result.extend(char.encode("utf-8"))
            index += 1
            continue
        mapping = {
            "a": 7,
            "b": 8,
            "f": 12,
            "n": 10,
            "r": 13,
            "t": 9,
            "v": 11,
            "\\": 92,
            '"': 34,
        }
        if nxt in mapping:
            result.append(mapping[nxt])
            index += 2
            continue
        result.extend(nxt.encode("utf-8"))
        index += 2
    try:
        return result.decode("utf-8")
    except UnicodeDecodeError:
        return ""


__all__ = ["change_file_paths", "decode_git_path", "safe_change_path"]
