"""Formal CLI review is read-only and restores an exact prior review from disk."""
import json
import subprocess

from codey.app import cli
from codey.env_names import REVIEW_POLICY_ENV
from tests.support.local_review_fixture import ScriptedLocal


def test_cli_review_cold_start_reuses_without_new_chat_or_send(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "app.py"),
                 ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    source.write_text("value = 2\n", encoding="utf-8")
    before = source.read_bytes()
    reviewers = [ScriptedLocal("fresh", [{"verdict": "approved", "summary": "Valid change", "findings": []}], []),
                 ScriptedLocal("reuse", [], [])]
    pending = iter(reviewers)
    monkeypatch.setenv(REVIEW_POLICY_ENV, "web_if_available")
    monkeypatch.setattr("codey.app.review_service.providers.reviewer_candidates", lambda *_: ())
    monkeypatch.setattr("codey.app.review_service.providers.connect_fresh_provider_tab", lambda *_: next(pending))
    options = ["agent", "--intent", "review", "--provider", "local", "--project", str(project),
               "--state-home", str(tmp_path / "state"), "--session-id", "s", "--json"]
    assert cli.main([*options, "Review changes"]) == 0
    first = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    original = next(row for row in first if row["type"] == "task_done")
    assert original["review"]["origin"] == "fresh"
    # run_headless creates and closes a new AppContext on every invocation;
    # the second invocation can only recover the result from official storage.
    assert cli.main([*options, "--review-source-run-id", original["run_id"], "Review changes"]) == 0
    second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    restored = next(row for row in second if row["type"] == "task_done")
    assert restored["review"]["origin"] == "reused"
    assert restored["review"]["status"] == "complete"
    assert len(reviewers[0].prompts) == 1
    assert reviewers[1].chats == 0 and reviewers[1].prompts == []
    assert source.read_bytes() == before
    assert not any(row["type"] in {"tool", "tool_started"} for row in first + second)
    assert restored.get("checks_passed") is not True
