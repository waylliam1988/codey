from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.review_service import run_review_attempt


class _FormatReviewer:
    name = "snapshot-fixture"
    location = "fixture"

    def __init__(self, reviewed_file: Path, *, change_snapshot: bool) -> None:
        self.reviewed_file = reviewed_file
        self.change_snapshot = change_snapshot
        self.send_count = 0

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        self.send_count += 1
        if self.send_count == 1:
            if self.change_snapshot:
                self.reviewed_file.write_text("updated = True\n", encoding="utf-8")
            return "Review response is not JSON."
        return '{"verdict":"approved","summary":"Reviewed","findings":[]}'


@pytest.mark.parametrize("change_snapshot", [True, False])
def test_format_repair_requires_current_review_snapshot(tmp_path: Path, change_snapshot: bool) -> None:
    project = tmp_path / "project"
    project.mkdir()
    reviewed_file = project / "app.py"
    reviewed_file.write_text("updated = False\n", encoding="utf-8")
    reviewer = _FormatReviewer(reviewed_file, change_snapshot=change_snapshot)
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    try:
        _, review = run_review_attempt(
            ctx, session_id="chat", project=str(project), task="Review the change",
            writer_summary="Updated the fixture", recent_log="", change_brief="",
            project_map="", verification_map="", review_impact_map="", execution_evidence="",
            changes={"ok": True, "files": [{"path": "app.py"}], "changed_count": 1,
                     "diff": "diff --git a/app.py b/app.py\n+updated = False\n"},
            reviewer_id="local", reviewer=reviewer, self_review=False,
        )
        if change_snapshot:
            assert (review.status, reviewer.send_count) == ("stale", 1)
            assert "snapshot_stale_before_format_repair" in review.diagnostics
        else:
            assert (review.status, reviewer.send_count) == ("complete", 2)
            assert review.approved
    finally:
        ctx.close()
