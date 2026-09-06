"""Shared workspace path and bounded file helpers."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path, PurePosixPath


def safe_join(root: str | Path, rel: str, *, label: str = "project root") -> Path:
    resolved_root = Path(root).expanduser().resolve()
    path = (resolved_root / str(rel)).resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise ValueError(f"path escapes {label}: {rel}")
    return path


def read_text_bounded(path: str | Path, *, max_bytes: int) -> str:
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"not a file: {target}")
    if target.stat().st_size > max(0, int(max_bytes)):
        raise ValueError(f"file too large for snapshot: {target}")
    return target.read_text(encoding="utf-8")


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
    target = Path(path)
    if not target.exists():
        return "missing"
    if not target.is_file():
        raise ValueError(f"not a file: {target}")
    digest = hashlib.sha256()
    with target.open("r", encoding="utf-8") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), ""):
            digest.update(chunk.encode("utf-8"))
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
    "is_test_path",
    "path_hash",
    "read_text_bounded",
    "read_text_or_none",
    "safe_join",
]
