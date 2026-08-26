from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from codey.workspace.map import render_project_map
from tests.manual.project_task_context import (
    production_candidate_command_lines,
    render_production_project_map,
)


def test_manual_production_project_map_uses_project_task_context_policy_candidates() -> None:
    with tempfile.TemporaryDirectory() as td, mock.patch(
        "codey.completion.verification_policy.shutil.which",
        return_value="exe",
    ):
        root = Path(td)
        (root / "package.json").write_text(
            '{"scripts":{"test":"vitest","lint":"eslint ."}}',
            encoding="utf-8",
        )
        (root / "pnpm-lock.yaml").write_text(
            "lockfileVersion: 9\n",
            encoding="utf-8",
        )

        production_map = render_production_project_map(root, task="change app")
        direct_map = render_project_map(root, task="change app")
        command_lines = production_candidate_command_lines(root, task="change app")

    assert "- pnpm test" in production_map
    assert "- pnpm run lint" in production_map
    assert "- npm test" not in production_map
    assert "Candidate commands" not in direct_map
    assert command_lines[:2] == ("pnpm test", "pnpm run lint")
