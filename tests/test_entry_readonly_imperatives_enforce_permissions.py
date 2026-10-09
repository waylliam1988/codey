"""Equivalent explicit no-edit instructions deny mutation at the shared entry."""
import pytest

from codey.task.entry_auth import derive_entry_auth


@pytest.mark.parametrize("instruction", ["must not change any file", "must not modify files",
    "must not edit files", "do not edit files", "don't edit any file"])
def test_explicit_no_edit_imperatives_deny_writes_and_preserve_verification(instruction):
    auth = derive_entry_auth({"intent": "project", "task":
        f"Diagnose and fix the defect. You may read files and run tests, but {instruction}."}, project="E:/fixture")
    assert "project.write" in auth.denied_capabilities
    assert "shell.approval" in auth.denied_capabilities
    assert "project.verify" not in auth.denied_capabilities
    assert auth.project_changes_required is False


@pytest.mark.parametrize("instruction", ["do not edit tests", "must not change app.py", "must not modify the test suite"])
def test_protecting_a_named_file_or_tests_does_not_deny_all_implementation_edits(instruction):
    auth = derive_entry_auth({"intent": "project", "task":
        f"Fix the implementation; {instruction}."}, project="E:/fixture")
    assert "project.write" not in auth.denied_capabilities
