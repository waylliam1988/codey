from __future__ import annotations

from pathlib import Path

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.coding_observations import verification_retry_guidance, verification_rows
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_prompt import working_context
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import ToolSuccessRequirement
from codey.runtime.core.models import ToolCall
from codey.workspace.coding_context import render_coding_context
from codey.workspace.revision import WorkspaceRevisionStore


def test_current_targeted_failure_overrides_full_suite_suggestion_and_links_receipt(tmp_path: Path) -> None:
    (tmp_path / "test_app.py").write_text("def test_value():\n    assert False\n", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"project.read", "project.verify"})),
                          project=str(tmp_path), coding_context_enabled=True)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    session.edited_files["app.py"] = 1
    session.selected_verification = VerificationCandidate("python -m pytest", ".")
    result = execute_turn(session, [ToolCall("run", {"command": "python -m pytest -q test_app.py", "path": "."})],
        project_path=tmp_path, run_id="feedback", turn=1, workspace_revision_store=revisions)[0]
    assert not result.ok, result.model_text
    rows = verification_rows(session)
    assert rows[-1]["state"] == "current_fail"
    ref = next(key for key, value in session._memory_results.items() if value is result)
    assert rows[-1]["result_ref"] == ref
    context = prepare_coding_context(session)
    assert context is not None
    rendered = render_coding_context(context)
    assert "failed against the current files" in rendered
    assert "Suggested verification" not in rendered
    assert ref in working_context(session)
    retry = verification_retry_guidance(result, session)
    assert ref in retry and "already failed against the current files" in retry
    session.set_workspace_state(session.workspace_revision + 1, "f" * 64)
    assert verification_rows(session)[-1]["state"] == "stale"
    assert not verification_retry_guidance(result, session)


def test_requirements_remain_visible_and_failed_execution_has_specific_feedback() -> None:
    requirement = ToolSuccessRequirement("read_file", arguments_json='{"path":"README.md"}')
    session = TaskSession(TaskPolicy(frozenset({"project.read"}), required_checks=(requirement.declaration,)))
    prompt = working_context(session)
    assert "declared_requirements" in prompt
    assert "README.md" in prompt and "Outstanding declared executions" in prompt
    assert "Resolve the outstanding requirements" in prompt


def test_incomplete_latest_verification_does_not_restore_old_success() -> None:
    session = TaskSession(TaskPolicy(frozenset({"project.verify"})))
    session.set_workspace_state(1, "a" * 64)
    session.verifications.extend([
        {"command": "python -m pytest", "cwd": ".", "revision": 0, "passed": True,
         "exit_code": 0, "workspace_revision": 1, "workspace_fingerprint": "a" * 64},
        {"command": "python -m pytest", "cwd": ".", "revision": 0, "passed": True},
    ])
    assert verification_rows(session) == [{"command": "python -m pytest", "cwd": ".", "state": "unknown"}]
