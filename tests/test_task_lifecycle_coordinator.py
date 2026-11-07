"""Behavior at the application task lifecycle boundary."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from codey.app.context import AppContext
from tests.support.shell_approval import add_pending_approval


def test_expiry_preserves_the_default_shell_result_output(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        add_pending_approval(ctx, "approval", {
            "id": "approval",
            "session_id": "session",
            "run_id": "run",
            "command": "echo pending",
            "cwd": str(tmp_path),
        })

        ctx.expire_pending_shell_approvals()

        result = ctx.run_registry.last_shell_result()
        assert result is not None
        assert result["output"] == "Task stopped; command approval expired."
        assert not ctx.pending_shell_approvals()
