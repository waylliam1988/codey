"""Preserving tests must not revoke requested implementation edits."""
import pytest

from codey.task.entry_auth import derive_entry_auth
from tools.local_model_release_gate import _task_for


@pytest.mark.parametrize("case", ["edit", "references", "hybrid", "tests"])
def test_live_gate_task_preserves_named_files_without_revoking_project_edits(case):
    task, intent, _ = _task_for(case)
    auth = derive_entry_auth({"task": task, "intent": intent, "project_changes_required": True}, project="project")
    assert "project.write" not in auth.denied_capabilities
    assert "shell.approval" not in auth.denied_capabilities
    assert auth.project_changes_required is True


@pytest.mark.parametrize("task", ["Explain the formula. Do not modify any files.",
                                  "Read-only review of the current implementation.",
                                  "只读检查，不修改任何文件。",
                                  "Do not modify tests or any other project files.",
                                  "Do not change calculator.py or any other file."])
def test_explicit_global_readonly_still_revokes_write_and_shell(task):
    auth = derive_entry_auth({"task": task, "intent": "project"}, project="project")
    assert {"project.write", "shell.approval"} <= set(auth.denied_capabilities)
