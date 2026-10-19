"""Allow and deny resume Chat in its original task and authorization."""
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
        submitted = Mock(return_value="unexpected-new-task")
        status, payload = shell_approval_response(state, {"id": approval_id, "approved": approved},
                                                 submit_task_after_slot_release=submitted)
        assert status == 202
        assert state.shell_approval_workflow.wait(5)
        assert state.shell_approval_workflow.status(approval_id)["continuation"] == "started"
        # Approval settlement wakes the original durable task.  It must not
        # create a continuation submission with a second task identity.
        assert submitted.call_count == 0
        assert state.shell_approval_workflow.status(approval_id)["continuation_run_id"] == record["run_id"]
        assert len(state.durable.tasks()) == 1
        task = state.durable.task(record["run_id"])
        assert task.state["project"] == (str(root) if attached else None)
        assert task.state["intent"] == "chat"
        assert not state.pending_shell_approvals()
        assert state.wait_for_ghost_sleep(timeout=30)
