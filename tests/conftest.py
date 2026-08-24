from __future__ import annotations

import os
from pathlib import Path


def _is_pytest_current_cleanup_permission_error(root: Path, exc: PermissionError) -> bool:
    try:
        root_parts = Path(root).parts
    except (OSError, TypeError, ValueError):
        root_parts = ()
    return "pytest-current" in root_parts or "pytest-current" in str(exc)


if os.name == "nt":
    import _pytest.pathlib as _pytest_pathlib

    _cleanup_dead_symlinks = _pytest_pathlib.cleanup_dead_symlinks

    def _windows_cleanup_dead_symlinks(root: Path) -> None:
        # Pytest may hit PermissionError resolving pytest-current symlinks on Windows.
        try:
            _cleanup_dead_symlinks(root)
        except PermissionError as exc:
            if _is_pytest_current_cleanup_permission_error(root, exc):
                return
            raise

    _pytest_pathlib.cleanup_dead_symlinks = _windows_cleanup_dead_symlinks
