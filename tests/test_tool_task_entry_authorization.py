"""Real coding tasks can preserve tests without becoming globally read only."""
import pytest

from codey.task.entry_auth import derive_entry_auth
from tools.tool_task_integration_fixture import TASK


@pytest.mark.parametrize("constraint", ["不要改测试、API 或数据库结构。", "不要修改 test_checkout.py。",
                                      "测试文件不要改动。", "Do not modify tests or api.py."])
def test_named_file_constraint_preserves_implementation_permission(constraint: str) -> None:
    auth = derive_entry_auth({"task": "修复 checkout.py。" + constraint, "intent": "project",
                              "project_changes_required": True}, project="project")
    assert not auth.denied_capabilities
    assert auth.project_changes_required


@pytest.mark.parametrize("task", ["修复 checkout.py，不要改测试或任何其他文件。",
                                  "不要修改 checkout.py 和其他文件。",
                                  "不要改测试，所有代码保持原样。",
                                  "不要改 checkout.py。其他文件也不要修改。",
                                  "只读检查，不修改任何文件。"])
def test_global_readonly_still_denies_write_and_shell(task: str) -> None:
    auth = derive_entry_auth({"task": task, "intent": "project"}, project="project")
    assert {"project.write", "shell.approval"} <= set(auth.denied_capabilities)


def test_real_integration_task_allows_requested_repair() -> None:
    auth = derive_entry_auth({"task": TASK, "intent": "project", "project_changes_required": True}, project="project")
    assert not auth.denied_capabilities
