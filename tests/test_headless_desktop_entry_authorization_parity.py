"""Both entry surfaces derive the same immutable task requirements from users."""
import pytest

from codey.app.api import derive_entry_auth
from codey.app.headless_runner import HeadlessRequest, run_headless


@pytest.mark.parametrize("task,intent", [
    ("Fix the bug", "project"),
    ("查官方文档并修复 bug", "project"),
    ("检查代码，不要修改任何文件", "project"),
    ("Find official evidence", "research"),
])
def test_headless_submission_preserves_the_same_authorization_as_desktop(tmp_path, monkeypatch, task, intent):
    captured = []
    monkeypatch.setattr("codey.app.headless_runner.run_task_submission", lambda deps, request: captured.append((deps, request)))
    project = tmp_path / "project"
    run_headless(HeadlessRequest(project=project, task=task, intent=intent, state_home=tmp_path / "state"), emit_jsonl=lambda _: None)
    expected = derive_entry_auth({"task": task, "intent": intent}, project=str(project))
    request = captured[0][1]
    for name in ("requested_capabilities", "strict_research", "sources_open_required", "project_changes_required", "denied_capabilities"):
        assert getattr(request, name) == getattr(expected, name), name
    deps = captured[0][0]
    assert callable(deps.run_consensus)
    assert callable(deps.run_project_audit)
    assert callable(deps.run_research_advisors)
