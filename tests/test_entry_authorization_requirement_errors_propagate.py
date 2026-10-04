"""A requirement derivation error must not silently remove task obligations."""
import pytest

from codey.task.entry_auth import derive_entry_auth


def test_requirement_error_cannot_downgrade_project_to_no_changes(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("requirement derivation unavailable")

    monkeypatch.setattr("codey.task.entry_auth.derive_project_changes_required", unavailable)
    with pytest.raises(RuntimeError, match="requirement derivation unavailable"):
        derive_entry_auth({"intent": "project", "task": "Fix bug"}, project="project")
