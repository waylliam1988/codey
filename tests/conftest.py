from __future__ import annotations

import os
from pathlib import Path


if os.name == "nt":
    import _pytest.pathlib as _pytest_pathlib

    _cleanup_dead_symlinks = _pytest_pathlib.cleanup_dead_symlinks

    def _windows_cleanup_dead_symlinks(root: Path) -> None:
        # Pytest may hit PermissionError resolving pytest-current symlinks on Windows.
        try:
            _cleanup_dead_symlinks(root)
        except PermissionError:
            pass

    _pytest_pathlib.cleanup_dead_symlinks = _windows_cleanup_dead_symlinks
