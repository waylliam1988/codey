"""A restarted writer completes recorded edits only after fresh verification."""
import threading
from types import SimpleNamespace

import pytest

from codey.agents.writer_failover import CheckpointView, WriterAttempt
from codey.operations.context import RunWork
from codey.operations.project_adapter import run
from codey.operations.project_completion_checks import _normalized_scope_and_change
from codey.operations.project_completion_context import ProjectRun
from codey.operations.project_writer_phase import _run_one_writer_attempt
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.revision import WorkspaceRevisionStore


def test_checkpoint_scope_is_combined_with_new_attempt_edits():
    session = TaskSession(policy=None)
    session.edited_files["adapter.py"] = 1
    scope, changed = _normalized_scope_and_change(session, {"scope_files": ("app.py",)})
    assert set(scope) == {"app.py", "adapter.py"}
    assert changed is True


@pytest.mark.parametrize("verify,expected", [(True, "done"), (False, "max_turns")])
def test_checkpoint_edit_scope_reaches_real_writer_gate_without_reediting(tmp_path, monkeypatch, verify, expected):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 2\n", encoding="utf-8")
    (project / "test_app.py").write_text("import unittest\nimport app\nclass Test(unittest.TestCase):\n"
        "    def test_value(self): self.assertEqual(app.value, 2)\n", encoding="utf-8")
    revisions = WorkspaceRevisionStore(tmp_path / "state")
    current = revisions.current_state(project)
    provider = ApiProvider("http://fixture.test/v1", "fixture")
    names = (["run"] if verify else []) + ["done"] * 6
    bodies = iter([{"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": f"call-{index}", "function": {"name": name,
         "arguments": '{"command":"python -m unittest discover -v"}' if name == "run" else '{"summary":"Finished prior work"}'}}
    ]}}]} for index, name in enumerate(names)])
    monkeypatch.setattr(provider, "_generate", lambda *a, **k: next(bodies))
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **k: True)
    monkeypatch.setattr("codey.operations.project_writer_phase._ghost_experiences", lambda *a, **k: "")
    policy = TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"}))
    ctx = ProjectRun(
        deps=SimpleNamespace(verification=SimpleNamespace(workspace_revisions=revisions),
            agent=SimpleNamespace(run=run), persistence=SimpleNamespace(managed_outputs=None),
            runtime=SimpleNamespace(mutations=None)),
        frame=SimpleNamespace(recovered_tool_outcomes=(), settled_tool_outcomes=(), recovered_tool_result_batch_id="",
            run_id="resume", conversation=None, provider_id="local", trace=None, project_text=str(project), entry_policy=policy),
        work=RunWork([], ExecutionEvidence(workspace_revision=current.revision, workspace_fingerprint=current.fingerprint)),
        hooks=SimpleNamespace(on_event=lambda event: None, on_shell_request=None),
        project=str(project), state=SimpleNamespace(run_registry=SimpleNamespace(stop_flag=threading.Event())),
        request=SimpleNamespace(session_id="s", max_turns=2, requested_capabilities=(), strict_research=False,
            project_changes_required=True), project_context=SimpleNamespace(research_context="", project_config_warnings=""))
    attempt = WriterAttempt("Finish the interrupted value fix", "local", provider, 2, False, "",
        CheckpointView(changed_files=("app.py",)))
    result = _run_one_writer_attempt(ctx, attempt, lambda turn: None)
    assert result.stop_reason == expected, result.summary
    assert (project / "app.py").read_text() == "value = 2\n"
    if verify:
        assert result.checks_passed
