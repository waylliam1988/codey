"""The formal headless review must call its configured provider and report failure honestly."""
from unittest.mock import patch

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


class Reviewer:
    name = "local"
    location = "test"

    def __init__(self, reply):
        self.reply = reply
        self.sends = []
        self.chats = 0

    def new_chat(self, timeout=None):
        self.chats += 1

    def send(self, text, timeout=None):
        self.sends.append(text)
        return self.reply

    def close(self):
        return True


def run_review(tmp_path, reviewer, *, changes_override=None):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("x = 2\n", encoding="utf-8")
    rows = []
    changes = {"ok": True, "files": [{"path": "app.py"}], "changed_count": 1,
               "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"}
    if changes_override is not None:
        changes = changes_override
    with patch("codey.app.review_service.providers.reviewer_candidates", return_value=()), patch(
               "codey.app.review_service.providers.connect_fresh_provider_tab",
               side_effect=AssertionError("must not use global browser connector")):
        result = run_headless(
            HeadlessRequest(project=project, task="Review changes", intent="review", provider_id="local",
                            state_home=tmp_path / "state"),
            connect_provider=lambda *_args, **_kwargs: reviewer,
            connect_reviewer=lambda *_args: reviewer,
            collect_changes=lambda *_args: changes,
            emit_jsonl=rows.append,
        )
    return result, rows


def test_formal_headless_review_uses_configured_connector_and_projects_final_result(tmp_path):
    reviewer = Reviewer('{"verdict":"approved","summary":"Looks good","findings":[]}')
    result, rows = run_review(tmp_path, reviewer)
    assert len(reviewer.sends) == 1
    assert result.exit_code == 0
    terminal = next(row for row in rows if row["type"] == "task_done")
    assert terminal["review"]["status"] == "complete"


def test_failed_review_cannot_report_successful_headless_task(tmp_path):
    reviewer = Reviewer("not JSON")
    result, rows = run_review(tmp_path, reviewer)
    assert len(reviewer.sends) == 2
    assert result.exit_code != 0
    assert next(row for row in rows if row["type"] == "task_done")["review"]["status"] == "unavailable"


def test_collect_failure_is_not_a_successful_noop_review(tmp_path):
    reviewer = Reviewer('{"verdict":"approved","findings":[]}')
    result, rows = run_review(tmp_path, reviewer, changes_override={"ok": False, "files": [], "diff": ""})
    assert result.exit_code != 0
    assert not reviewer.sends
    terminal = next(row for row in rows if row["type"] == "task_done")
    assert terminal["review"]["status"] == "unavailable"
