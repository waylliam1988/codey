"""Allow and deny resume Chat in its original task and authorization."""

import copy
import json
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import pytest

from codey.app.api import shell_approval_response
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.task_entry import run_task_submission
from codey.providers import api_generation
from codey.storage.local_store import session_key
from codey.task.model import TaskSubmission
from tests.support.workspace_conversation import TextConversation, short_test_state_home

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("attached", [False, True])
def test_shell_decision_resumes_chat_without_project_workflow(tmp_path, monkeypatch, approved, attached):
    root = tmp_path / "project"
    root.mkdir()
    home = short_test_state_home()
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
        assert Path(record["project"]).resolve() == workspace.resolve()
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


@pytest.mark.parametrize("approved", [False, True])
def test_native_approval_resume_keeps_original_task_and_tool_history(tmp_path, monkeypatch, approved):
    from codey.app import shell_service
    from codey.providers.api_provider import ApiProvider

    payloads = []
    task = "Run echo approval-marker once and report its result."
    command = "echo approval-marker"

    def generate(_url, payload, *_args, **_kwargs):
        payloads.append(copy.deepcopy(payload))
        name, args = (("shell", {"command": command, "path": "."}) if len(payloads) == 1
                      else ("done", {"summary": "The command result has been received."}))
        return {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": f"call-{len(payloads)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}, "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    execute = Mock(return_value={"ok": True, "status": "exit", "output": "approval-marker", "exit_code": 0})
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    provider = ApiProvider("http://scripted.test/v1", "scripted-fixture", native_tools=True, stream=False)
    with closing(AppContext(short_test_state_home())) as state:
        shell_events = []
        starts = []
        state.event_bus.observe(lambda _sequence, event: shell_events.append(event)
                                if event.get("type") == "shell_result" else None)
        state.event_bus.observe(lambda _sequence, event: starts.append(event)
                                if event.get("type") == "task_start" else None)
        monkeypatch.setattr(state, "get_provider", lambda *args: provider)
        run_task_submission(build_task_deps(state), TaskSubmission(
            "native-shell", None, task, 3, False, "local", intent="chat"))
        approval_id, record = next(iter(state.pending_shell_approvals().items()))
        workers = []
        queued = threading.Event()

        def submit(fn, *args, **kwargs):
            workers.append(lambda: fn(*args, **kwargs))
            queued.set()
            return True

        monkeypatch.setattr("codey.app.task_submit.submit_browser_task", submit)
        status, _ = shell_approval_response(state, {"id": approval_id, "approved": approved},
                                           submit_task_after_slot_release=Mock())
        assert status == 202
        assert state.shell_approval_workflow.wait(5)
        assert queued.wait(5)
        assert len(workers) == 1
        workers.pop()()
        assert state.wait_for_ghost_sleep(timeout=30)
        assert len(payloads) == 2
        messages = payloads[1]["messages"]
        calls = [call for message in messages for call in message.get("tool_calls", [])]
        results = [message for message in messages if message["role"] == "tool"]
        assert [call["id"] for call in calls] == ["call-1"]
        assert len(results) == 1 and results[0]["tool_call_id"] == "call-1"
        assert ("approval-marker" if approved else "Denied by user") in results[0]["content"]
        observations = [message["content"] for message in messages
                        if "Current work observations (runtime records):" in str(message.get("content"))]
        assert len(observations) == 1
        assert f'"user_task": "{task}"' in observations[0]
        assert "Resolve the shell approval" not in observations[0]
        assert execute.call_count == int(approved)
        assert len(starts) == 2 and all(event["task"] == task for event in starts)
        from codey.app.public_event_feed import PublicEventFeed

        feed = PublicEventFeed()
        assert len(shell_events) == (2 if approved else 1)
        for sequence, event in enumerate(shell_events, start=1):
            public = json.loads(feed.encode(event, sequence).body)
            assert public["call_id"] == "call-1"
            assert public["run_id"] == record["run_id"]
        assert len(state.durable.tasks()) == 1
        assert state.durable.task(record["run_id"]).state["task"] == task
