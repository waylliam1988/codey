"""Allow and deny resume Chat in its original task and authorization."""

import json
import threading
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

import pytest

from codey.app.api import shell_approval_response
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.ghost.payload_types import object_fields
from codey.operations.task_entry import run_task_submission
from codey.providers import api_generation
from codey.providers.token_accounting import ContextBudget
from codey.storage.local_store import session_key
from codey.task.model import TaskSubmission
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from tests.support.workspace_conversation import short_test_state_home


class _ShellConversation:
    native_tools = False
    name = "Local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def __init__(self, reply: str) -> None:
        self.replies = iter((reply,))

    def bind_usage(self, connection_id: str, sink: object) -> None:
        pass

    def new_chat(self) -> None:
        pass

    def send(self, prompt: str) -> str:
        return next(self.replies)

    def close(self) -> None:
        pass


def _unexpected_submission(*_args: object, **_kwargs: object) -> str | None:
    raise AssertionError("approval must wake the original task, never submit another")


def _pending(state: AppContext) -> dict[str, dict[str, object]]:
    raw: object = state.pending_shell_approvals()
    assert object_fields(raw)
    pending: dict[str, dict[str, object]] = {}
    for key, record in raw.items():
        assert object_fields(record)
        pending[key] = record
    return pending


def _decision(state: AppContext, approval_id: str, approved: bool) -> int:
    body: dict[str, object] = {"id": approval_id, "approved": approved}
    response: object = shell_approval_response(
        state, body,
        submit_task_after_slot_release=_unexpected_submission,
    )
    assert isinstance(response, tuple) and len(response) == 2
    status = tuple[object, ...](response)[0]
    assert isinstance(status, int)
    return status


pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("attached", [False, True])
def test_shell_decision_resumes_chat_without_project_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved: bool, attached: bool,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    home = short_test_state_home()
    provider = _ShellConversation('{"tool":"shell","args":{"command":"echo workspace-approved","path":"."}}')
    with closing(AppContext(home)) as state:
        def get_provider(_provider_id: str) -> _ShellConversation:
            return provider

        monkeypatch.setattr(state, "get_provider", get_provider)
        run_task_submission(build_task_deps(state), TaskSubmission(
            "chat-shell", str(root) if attached else None, "Run echo workspace-approved", 3, False, "local",
            intent="chat"))
        pending = _pending(state)
        assert len(pending) == 1
        approval_id, record = next(iter(pending.items()))
        workspace = root if attached else home / "workspaces" / session_key("chat-shell")
        recorded_project = record["project"]
        assert isinstance(recorded_project, str)
        assert Path(recorded_project).resolve() == workspace.resolve()
        status = _decision(state, approval_id, approved)
        assert status == 202
        assert state.shell_approval_workflow.wait(5)
        workflow_status = state.shell_approval_workflow.status(approval_id)
        assert workflow_status is not None
        assert workflow_status["continuation"] == "started"
        # Approval settlement wakes the original durable task.  It must not
        # create a continuation submission with a second task identity.
        assert workflow_status["continuation_run_id"] == record["run_id"]
        assert len(state.durable.tasks()) == 1
        run_id = record["run_id"]
        assert isinstance(run_id, str)
        task = state.durable.task(run_id)
        assert task.state["project"] == (str(root) if attached else None)
        assert task.state["intent"] == "chat"
        assert not _pending(state)
        assert state.wait_for_ghost_sleep(timeout=30)


@pytest.mark.parametrize("approved", [False, True])
def test_native_approval_resume_keeps_original_task_and_tool_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved: bool,
) -> None:
    from codey.app import shell_service
    from codey.providers.api_provider import ApiProvider

    payloads: list[JsonObject] = []
    task = "Run echo approval-marker once and report its result."
    command = "echo approval-marker"

    def generate(_url: str, payload: Mapping[str, object],
                 *_args: object, **_kwargs: object) -> JsonObject:
        payloads.append(canonical_json_object(dict(payload)))
        name, args = (("shell", {"command": command, "path": "."}) if len(payloads) == 1
                      else ("done", {"summary": "The command result has been received."}))
        return {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": f"call-{len(payloads)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}, "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    executions: list[shell_service.ShellExecutionTicket] = []

    def execute(_ctx: shell_service.ShellExecutionContext,
                ticket: shell_service.ShellExecutionTicket) -> shell_service.ShellResult:
        executions.append(ticket)
        return {"ok": True, "status": "exit", "output": "approval-marker", "exit_code": 0}

    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)

    def get_provider(_provider_id: str) -> ApiProvider:
        # Durable recovery restores protocol history into a fresh connection.
        return ApiProvider("http://scripted.test/v1", "scripted-fixture", native_tools=True, stream=False)
    with closing(AppContext(short_test_state_home())) as state:
        shell_events: list[dict[str, object]] = []
        starts: list[dict[str, object]] = []
        resumed_done = threading.Event()

        def observe(_sequence: int, event: object) -> None:
            assert object_fields(event)
            if event.get("type") == "shell_result":
                shell_events.append(dict(event))
            elif event.get("type") == "task_start":
                starts.append(dict(event))
            elif event.get("type") == "task_done" and event.get("stop_reason") != "approval":
                resumed_done.set()

        state.event_bus.observe(observe)
        monkeypatch.setattr(state, "get_provider", get_provider)
        run_task_submission(build_task_deps(state), TaskSubmission(
            "native-shell", None, task, 3, False, "local", intent="chat"))
        approval_id, record = next(iter(_pending(state).items()))
        status = _decision(state, approval_id, approved)
        assert status == 202
        assert state.shell_approval_workflow.wait(5)
        workflow_status = state.shell_approval_workflow.status(approval_id)
        assert workflow_status is not None
        assert workflow_status["continuation"] == "started"
        assert resumed_done.wait(10)
        assert state.wait_for_ghost_sleep(timeout=30)
        assert len(payloads) == 2
        raw_messages = payloads[1]["messages"]
        assert isinstance(raw_messages, list)
        messages = [decode_json_object(message) for message in raw_messages]
        calls: list[JsonObject] = []
        for message in messages:
            raw_calls = message.get("tool_calls", [])
            assert isinstance(raw_calls, list)
            calls.extend(decode_json_object(call) for call in raw_calls)
        results = [message for message in messages if message["role"] == "tool"]
        assert [call["id"] for call in calls] == ["call-1"]
        assert len(results) == 1 and results[0]["tool_call_id"] == "call-1"
        content = results[0]["content"]
        assert isinstance(content, str)
        assert ("approval-marker" if approved else "Denied by user") in content
        observations = [message["content"] for message in messages
                        if "Current work observations (runtime records):" in str(message.get("content"))]
        assert len(observations) == 1
        observation = observations[0]
        assert isinstance(observation, str)
        assert f'"user_task": "{task}"' in observation
        assert "Resolve the shell approval" not in observation
        assert len(executions) == int(approved)
        assert len(starts) == 2 and all(event["task"] == task for event in starts)
        from codey.app.public_event_feed import PublicEventFeed

        feed = PublicEventFeed()
        assert len(shell_events) == (2 if approved else 1)
        for sequence, event in enumerate(shell_events, start=1):
            raw_public: object = json.loads(feed.encode(event, sequence).body)
            public = decode_json_object(raw_public)
            assert public["call_id"] == "call-1"
            assert public["run_id"] == record["run_id"]
        assert len(state.durable.tasks()) == 1
        run_id = record["run_id"]
        assert isinstance(run_id, str)
        assert state.durable.task(run_id).state["task"] == task
