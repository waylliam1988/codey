"""A declared edit requirement must produce a pass as well as a failure."""
from __future__ import annotations

import json

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.revision import workspace_fingerprint

pytestmark = pytest.mark.usefixtures("no_external_advisor_models")


@pytest.mark.parametrize("with_context", [False, True])
@pytest.mark.parametrize("changed,verified", [(False, True), (True, False), (True, True)])
def test_required_edit_produces_check_without_excusing_verification(tmp_path, with_context, changed, verified):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(
            grants=frozenset({"control", "project.read", "project.write", "project.verify"}),
            required_checks=("project_changes_required",),
        ), task_kind="project", project=str(tmp_path), max_turns=4,
    )
    session.set_workspace_state(1, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate("python -m pytest", ".", "project")
    if changed:
        session.record_edit("a.py", revision=1)
    if verified:
        session.record_verification(
            "python -m pytest", 1, True, exit_code=0, cwd=".",
            workspace_revision=1, workspace_fingerprint=session.workspace_fingerprint,
        )
    context = None
    if with_context:
        context = {"run_id": "required-edit", "project": str(tmp_path),
                   "scope_files": ("a.py",) if changed else (),
                   "execution_evidence": ExecutionEvidence(
                       workspace_revision=1, workspace_fingerprint=session.workspace_fingerprint)}
    verdict = evaluate(session, "Changed the file and all tests passed.", context=context)
    assert verdict.complete is (changed and verified), verdict.followup
    if changed and verified:
        checks = [check for check in verdict.proof.checks if check.check_id == "project_changes_required"]
        assert len(checks) == 1 and checks[0].status == "pass"


def test_real_headless_required_edit_and_fresh_verification_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("x = 0\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_a.py").write_text(
        "import unittest\nfrom a import x\nclass Tests(unittest.TestCase):\n"
        "    def test_x(self):\n        self.assertEqual(x, 1)\n", encoding="utf-8",
    )
    replies = iter([
        {"tool": "read_file", "args": {"path": "a.py"}},
        {"tool": "edit", "args": {"path": "a.py", "replacements": [{"old_string": "x = 0", "new_string": "x = 1"}]}},
        {"tool": "run", "args": {"command": "python -m unittest discover", "path": "."}},
        {"tool": "done", "args": {"summary": "Fixed x and verified tests."}},
    ])
    class Provider:
        name = "Local"
        def new_chat(self):
            pass
        def close(self):
            pass
        def send(self, text, timeout=None):
            return json.dumps(next(replies))
    rows = []
    result = run_headless(
        HeadlessRequest(project=project, state_home=tmp_path / "state", provider_id="local", intent="project",
                        task="Fix a.py and run the tests.", max_turns=4, project_changes_required=True),
        emit_jsonl=rows.append,
        connect_provider=lambda *a, **kw: Provider(),
        connect_reviewer=lambda *a, **kw: Provider(),
    )
    assert result.stop_reason == "done", rows
    assert result.exit_code == 0
    assert (project / "a.py").read_text() == "x = 1\n"
    assert any(row.get("tool_name") == "run" and row.get("exit_code") == 0 for row in rows)
