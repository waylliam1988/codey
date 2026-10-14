from __future__ import annotations

import subprocess
from pathlib import Path

from codey.workspace.changes import collect_git_file_diff


def test_collect_git_file_diff_returns_independent_large_file_preview(tmp_path: Path) -> None:
    (tmp_path / "large.js").write_text("x" * 130_000, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # The helper must return a per-file reason even when the aggregate change
    # response would have to truncate its combined diff.
    result = collect_git_file_diff(tmp_path, "large.js")
    assert result["ok"] is True
    assert result["status"] == "too_large"
    assert result["size"] > 120_000


def test_changes_file_response_rejects_missing_scope() -> None:
    from codey.app.api import changes_file_response

    status, payload = changes_file_response(object(), {})
    assert status == 400
    assert payload["error"] == "project and path required"
