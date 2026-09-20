"""Shared discovery small tools for completion verification.

Neither the candidate map (what the model sees) nor the trusted selection
policy owns these twice: excluded-directory vocabulary, contained-cwd
resolution, and bounded manifest reads live here, once. The two layers keep
their own budgets, walkers, candidate types, and decisions -- this module
never discovers, selects, or proves anything on its own.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from codey.workspace.map import EXCLUDED_DIRS as _MAP_EXCLUDED_DIRS


TRUSTED_EXCLUDED_DIRS = _MAP_EXCLUDED_DIRS
"""Directory names never descended during trusted verification discovery.

Single vocabulary shared with the workspace map (which the candidate map
already uses). One deliberate delta versus the old policy-local subset:
``coverage/`` is now skipped too -- coverage output is never a trustworthy
verification root.
"""


def safe_cwd(root: Path, value: object) -> str | None:
    """Contained cwd relative to ``root``, or None when it escapes."""
    text = str(value or ".").strip().replace("\\", "/") or "."
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    target = (root / path).resolve()
    if root != target and root not in target.parents:
        return None
    return path.as_posix()


def is_manifest_file(path: Path) -> bool:
    """A real file that may be read as a manifest (never a symlink)."""
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def read_manifest_text(path: Path, *, max_bytes: int) -> str:
    """Best-effort bounded manifest read; failure means empty, never raise."""
    try:
        if not is_manifest_file(path):
            return ""
        if path.stat().st_size > max(0, int(max_bytes or 0)):
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def is_real_directory(path: Path) -> bool:
    """A real directory (never a symlink); I/O failure means False."""
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


__all__ = [
    "TRUSTED_EXCLUDED_DIRS",
    "is_manifest_file",
    "is_real_directory",
    "read_manifest_text",
    "safe_cwd",
]
