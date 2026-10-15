"""Allow and Deny resume Chat in its original workspace and authorization."""
from contextlib import closing
from unittest.mock import Mock

import pytest

from codey.app.api import shell_approval_response
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.task_entry import run_task_submission
from codey.storage.local_store import session_key
from codey.task.model import TaskSubmission
from tests.support.workspace_conversation import TextConversation

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("attached", [False, True])
def test_shell_decision_resumes_chat_without_project_workflow(tmp_path, monkeypatch, approved, attached):
    root = tmp_path / "project"
    root.mkdir()
    home = tmp_path / "state"
    provider = TextConversation('{"tool":"shell","args":{"command":"echo workspace-approved","path":"."}}')
    with closing(AppContext(home)) as state:
        monkeypatch.setattr(state, "get_provider", lambda *args: provider)
        run_task_submission(build_task_deps(state), TaskSubmission(
            "chat-shell", str(root) if attached else None, "Run echo workspace-approved", 3, False, "local",
            intent="chat"))
        pending = state.pending_shell_approvals()
        assert len(pending) == 1
        approval_id, record = next(iter(pending.items()))
        workspace = root if attached else home / "workspaces" / session_key("chat-shell")
        assert record["project"] == str(workspace)
        submitted = Mock(return_value="continued-run")
        status, payload = shell_approval_response(state, {"id": approval_id, "approved": approved},
                                                 submit_task_after_slot_release=submitted)
        assert status == 202
        assert state.shell_approval_workflow.wait(5)
        assert state.shell_approval_workflow.status(approval_id)["continuation"] == "started"
        args = submitted.call_args.args
        assert args[1] == (str(root) if attached else None)
        assert args[6] == "chat"
        assert submitted.call_args.kwargs["previous_run_id"] == record["run_id"]
        assert not state.pending_shell_approvals()
        assert state.wait_for_ghost_sleep(timeout=30)
