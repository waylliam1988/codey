"""Applying an observed plan does not turn its source query into a write."""
from __future__ import annotations

import pytest

from codey.task.entry_auth import derive_entry_auth


def test_explicit_plan_application_keeps_ordinary_edit_authorization() -> None:
    auth = derive_entry_auth({"intent": "project", "task": (
        "Rename total to doubled. Apply its read-only plan through tools.edit; verify the final files.")}, project="D:/fixture")
    assert "project.write" not in auth.denied_capabilities and auth.project_changes_required


@pytest.mark.parametrize("task", [
    "Provide a read-only plan for renaming total.",
    "Use read-only queries to inspect total.",
    "Apply its read-only plan through tools.edit. Do not modify files.",
    "Read-only: inspect the plan, do not apply it.",
])
def test_actual_readonly_constraints_remain_denied(task: str) -> None:
    auth = derive_entry_auth({"intent": "project", "task": task}, project="D:/fixture")
    assert "project.write" in auth.denied_capabilities and not auth.project_changes_required
