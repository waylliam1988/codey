"""A named edit allowance gives 'other files' its local meaning."""
import pytest

from codey.task.entry_auth import derive_entry_auth


@pytest.mark.parametrize("task", [
    "Repair checkout.py. Change only checkout.py; do not change tests, API, database schema or other project files.",
    "Only edit checkout.py. Do not modify tests or any other project files.",
    "Change only checkout.py; do not modify tests or other files. Then inspect checkout.py.",
    "只修改 checkout.py，其他文件不要改动。",
    "只修改 checkout.py；不要修改测试和其他文件。",
])
def test_local_preservation_does_not_revoke_explicit_edit(task: str) -> None:
    auth = derive_entry_auth({"intent": "project", "task": task, "project_changes_required": True}, project="p")
    assert not auth.denied_capabilities
    assert auth.project_changes_required


@pytest.mark.parametrize("task", [
    "Do not modify tests or any other project files.",
    "Do not change checkout.py or any other file.",
    "Change only checkout.py. Do not modify any files.",
    "Change only checkout.py. Do not change checkout.py or any other file.",
    "Change only the checkout.py. Do not modify tests, checkout.py or any other files.",
    "Change only checkout.py. Read-only review.",
    "Do not change only checkout.py or other files.",
    "只修改 checkout.py；不修改任何文件。",
    "只修改 checkout.py；只读检查。",
])
def test_actual_global_readonly_still_denies_write(task: str) -> None:
    auth = derive_entry_auth({"intent": "project", "task": task}, project="p")
    assert {"project.write", "shell.approval"} <= set(auth.denied_capabilities)
