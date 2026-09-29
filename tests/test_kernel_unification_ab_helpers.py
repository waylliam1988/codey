from __future__ import annotations

import sys
from pathlib import Path

from tests.manual.kernel_unification_ab import (
    LEGACY_COMMIT,
    _codey_command,
    _source_root_for_arm,
)


def test_kernel_unification_ab_uses_requested_legacy_commit() -> None:
    assert LEGACY_COMMIT == "958bcb485bf05d0ae8232763681d1df5ecee1d34"


def test_kernel_unification_ab_resolves_distinct_source_roots(tmp_path: Path) -> None:
    unified = tmp_path / "unified"
    legacy = tmp_path / "legacy"
    assert _source_root_for_arm("codey_unified", unified, legacy) == unified.resolve()
    assert _source_root_for_arm("codey_pre_unified", unified, legacy) == legacy.resolve()


def test_kernel_unification_ab_runs_same_agent_command_for_both_arms(tmp_path: Path) -> None:
    command = _codey_command(tmp_path / "project", tmp_path / "state", 8)
    assert command[:7] == [
        sys.executable,
        "-m",
        "codey",
        "agent",
        "--json",
        "--provider",
        "local",
    ]
    assert command[-1].startswith("Fix app.py")
