"""Shared services preserve review policy and optional project authorization."""
import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.policies.task_policy import build_task_policy


@pytest.mark.parametrize("intent", ["chat", "research", "auto"])
def test_no_project_submission_never_grants_project_tools(tmp_path, monkeypatch, intent):
    submissions = []
    monkeypatch.setattr("codey.app.headless_runner.run_task_submission", lambda _deps, request: submissions.append(request))
    run_headless(HeadlessRequest(project=None, task="Inspect", intent=intent, state_home=tmp_path / "state"), emit_jsonl=lambda _: None)
    request = submissions[0]
    assert request.project is None
    policy = build_task_policy(request, task_kind=intent)
    assert not any(policy.allows(capability) for capability in ("project.read", "project.write", "project.verify"))


@pytest.mark.parametrize("kwargs", [{"intent": "unknown"}, {"review_policy": "typo"}])
def test_invalid_task_configuration_does_not_create_project_or_state(tmp_path, kwargs):
    project, state = tmp_path / "project", tmp_path / "state"
    with pytest.raises(ValueError):
        run_headless(HeadlessRequest(project=project, task="Fix bug", state_home=state, **kwargs), emit_jsonl=lambda _: None)
    assert not project.exists() and not state.exists()


def test_require_web_without_web_reviewer_never_self_reviews(tmp_path, monkeypatch, scripted_local_api_connection):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr("codey.app.review_service.providers.reviewer_candidates", lambda *_: ())
    monkeypatch.setattr("codey.app.review_service.providers.connect_fresh_provider_tab", lambda *_: pytest.fail("must not self-review"))
    rows = []
    result = run_headless(HeadlessRequest(project=project, task="Review", intent="review", provider_id="local",
                                         review_policy="require_web", state_home=tmp_path / "state"), emit_jsonl=rows.append,
                          collect_changes=lambda *_: {"ok": True, "changed_count": 1, "files": [{"path": "app.py"}],
                                                      "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"})
    assert result.exit_code != 0
    assert next(row for row in rows if row["type"] == "task_done")["review"]["status"] == "unavailable"
