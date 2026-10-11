"""Chat uses the same explicit task constraints as the common workspace path."""
import pytest

from codey.task.entry_auth import derive_entry_auth


@pytest.mark.parametrize("intent", ["chat", "auto"])
def test_explicit_readonly_chat_denies_write_and_shell(intent):
    auth = derive_entry_auth({"intent": intent, "task": "只检查代码，不要修改文件"}, project=None)
    assert set(auth.denied_capabilities) >= {"project.write", "shell.approval"}


@pytest.mark.parametrize("intent", ["chat", "auto"])
def test_explicit_web_request_in_chat_preserves_source_requirement(intent):
    auth = derive_entry_auth({"intent": intent, "task": "上网搜索官方文档"}, project=None)
    assert "web.read" in auth.requested_capabilities
    assert auth.sources_open_required


def test_workspace_submission_rejects_retired_auto_plan_field():
    from codey.task.model import TaskSubmission

    with pytest.raises(TypeError, match="model_hint"):
        TaskSubmission("s", None, "hello", 2, False, "local", model_hint="route this task")
