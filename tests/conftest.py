from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path


def _install_isolated_state_home() -> Path:
    """Give every pytest process a writable home before application imports.

    Codey's default stores are module constants derived from ``Path.home()``.
    This must run while conftest is imported, before test collection imports
    application modules; a later fixture would leave cached defaults pointing
    at the real user profile.
    """
    existing = os.environ.get("PYTEST_STATE_HOME")
    if existing:
        root = Path(existing).resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="codey-pytest-home-")).resolve()
        os.environ["PYTEST_STATE_HOME"] = str(root)
        atexit.register(lambda: _remove_test_home(root))
    root.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(root)
    if os.name == "nt":
        os.environ["USERPROFILE"] = str(root)
        os.environ["HOMEDRIVE"] = root.drive
        os.environ["HOMEPATH"] = root.anchor[len(root.drive):] or "\\"
    return root


def _remove_test_home(root: Path) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)


_PYTEST_STATE_HOME = _install_isolated_state_home()


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
