from __future__ import annotations

from pathlib import Path

from tests.conftest import _is_pytest_current_cleanup_permission_error


def test_pytest_cleanup_guard_matches_only_pytest_current() -> None:
    assert _is_pytest_current_cleanup_permission_error(
        Path("C:/Temp/pytest-of-user/pytest-current"),
        PermissionError("access denied"),
    )
    assert _is_pytest_current_cleanup_permission_error(
        Path("C:/Temp/pytest-of-user/pytest-42"),
        PermissionError("access denied: pytest-current"),
    )
    assert not _is_pytest_current_cleanup_permission_error(
        Path("C:/Temp/pytest-of-user/pytest-42"),
        PermissionError("access denied"),
    )
