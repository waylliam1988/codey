"""Chat uses real workspace tools and the same completion gate as project work."""

import hashlib
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codey.agents.handoff import ConversationContext
from codey.app.context import AppContext
from codey.app.memory_routes import memory_action
from codey.operations.context import RunFrame, RunWork
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes
from codey.workspace.context_admission import request_scope
from tests.support.kernel_protocol_test_helpers import normalize_turn
from tests.support.kernel_runtime import run_entry_kernel


def _state_home(tmp_path):
    """Keep durable checkpoint paths below Windows' legacy path limit."""
    digest = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    return tmp_path.parent / digest


class NativeConversation:
    native_tools = True
    requests: list[tuple[str, list[ProviderToolDefinition]]]

    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []
        self.results = []
        self.resets = 0

    def new_chat(self):
        self.resets += 1

    def send_turn(self, prompt, tools):
        self.requests.append((prompt, tools))
        return next(self.turns)

    def send_tool_results(self, results, tools):
        self.results.extend(results)
        return self.send_turn("tool results", tools)

    def acknowledge_tool_results(self, results, tools):
        self.results.extend(results)
        return AssistantTurn()


def workspace_frame(root, provider, *, intent="chat", task="Hello", **requirements):
    request = TaskSubmission("chat-workspace", None, task, 4, False, "local",
                             intent=intent, run_id=f"workspace-{uuid4().hex}", **requirements)
    return RunFrame(request, request.run_id, intent, provider, "local", str(root),
                    ConversationContext(), True, "", "", None, "", False, set(), 0)


def admit_frame(frame, state):
    from tests.support.workspace_conversation import admit_entry_frame

    admit_entry_frame(frame, state)


def execute_frame(frame, state):
    from tests.support.kernel_runtime import approval_notifier

    admit_frame(frame, state)
    events = []
    deps = SimpleNamespace(state=state, workspace_revisions=state.workspace_revisions,
                           collect_changes=collect_changes)
    result = run_entry_kernel(frame, RunWork([], ExecutionEvidence()),
                              SimpleNamespace(on_event=events.append, on_shell_request=approval_notifier(
                                  state.durable, state.managed_outputs, frame.request.session_id, frame.run_id,
                                  events.append)), deps)
    return result, events


@pytest.mark.parametrize("intent", ["chat", "auto"])
def test_chat_creates_workspace_file_and_finishes_with_natural_answer(tmp_path, intent):
    root = tmp_path / "workspace"
    root.mkdir()
    provider = NativeConversation(
        AssistantTurn(tool_calls=(ProviderToolCall("create", "edit", {
            "path": "result.txt", "content": "actual file"}),)),
        AssistantTurn(text="Created result.txt."),
    )
    frame = workspace_frame(root, provider, intent=intent, task="Create result.txt; do not run commands.",
                            denied_capabilities=("project.verify", "shell.approval"))
    with closing(AppContext(_state_home(tmp_path))) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done"
    assert result.event["turns"] == 2
    assert (root / "result.txt").read_text(encoding="utf8") == "actual file"
    assert [row.call_id for row in provider.results] == ["create"]
    assert provider.resets == 1


def test_native_greeting_finishes_in_one_request_with_workspace_tools_available(tmp_path):
    provider = NativeConversation(AssistantTurn(text="Hello!"))
    frame = workspace_frame(tmp_path / "workspace", provider)
    with closing(AppContext(_state_home(tmp_path))) as state:
        result, events = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done"
    assert result.event["summary"] == "Hello!"
    assert len(provider.requests) == 1
    assert {tool.name for tool in provider.requests[0][1]} >= {"read_file", "edit", "shell", "run"}
    assert not provider.results
    assert not any(getattr(event, "kind", "") == "tool" for event in events)


def test_native_json_example_is_an_answer_and_never_a_tool_call():
    text = '{"tool":"shell","args":{"command":"example","path":"."}}'
    plan = normalize_turn(AssistantTurn(text=text), policy=TaskPolicy(frozenset({"control", "shell.approval"})))
    assert plan.calls == []
    assert plan.control.kind == "done"
    assert plan.control.body == text


def test_natural_answer_cannot_bypass_required_project_changes(tmp_path):
    provider = NativeConversation(AssistantTurn(text="Already finished."), AssistantTurn(text="Still finished."))
    frame = workspace_frame(tmp_path, provider, project_changes_required=True)
    frame.request = replace(frame.request, max_turns=2)
    with closing(AppContext(_state_home(tmp_path))) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] != "done"
    assert not frame.entry_session.edited_files


def test_unbound_chat_writes_only_in_its_managed_session_workspace(tmp_path):
    provider = NativeConversation(
        AssistantTurn(tool_calls=(ProviderToolCall("script", "edit", {
            "path": "inspect.py", "content": "print('observed')\n"}),)),
        AssistantTurn(text="Created inspect.py."),
    )
    frame = workspace_frame("", provider, task="Write a script; do not run commands.",
                            denied_capabilities=("project.verify", "shell.approval"))
    state_home = _state_home(tmp_path)
    with closing(AppContext(state_home)) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done"
    root = Path(frame.project_text)
    assert root.parent == state_home / "workspaces"
    assert (root / "inspect.py").read_text(encoding="utf8") == "print('observed')\n"
    assert frame.request.project is None
    assert result.event["mode"] == "chat"


def test_unbound_chat_shell_uses_real_approval_without_starting_a_process(tmp_path):
    provider = NativeConversation(AssistantTurn(tool_calls=(
        ProviderToolCall("inspect", "shell", {"path": ".", "command": "python --version"}),)))
    frame = workspace_frame("", provider, task="Check Python version")
    with closing(AppContext(_state_home(tmp_path))) as state:
        result, events = execute_frame(frame, state)
    assert result.event["stop_reason"] == "approval"
    approval = next(event for event in events if getattr(event, "call_id", "") == "inspect")
    assert approval.command == "python --version"
    assert frame.entry_session.project == frame.project_text
    assert not provider.results


def test_json_code_block_example_is_not_executed():
    text = '```json\n{"tool":"shell","args":{"command":"example","path":"."}}\n```'
    plan = normalize_turn(text, policy=TaskPolicy(frozenset({"control", "shell.approval"})))
    assert plan.calls == []
    assert plan.control.body == text


def test_malformed_text_tool_call_is_not_delivered_as_successful_answer():
    plan = normalize_turn('{"tool":"shell","args":', policy=TaskPolicy(frozenset({"control"})))
    assert plan.protocol_error
    assert not plan.control


def test_json_data_answer_is_not_an_unknown_tool():
    text = '{"python": "a language", "versions": [2, 3]}'
    plan = normalize_turn(text, policy=TaskPolicy(frozenset({"control"})))
    assert not plan.protocol_error
    assert plan.control.body == text


def test_auto_with_attached_folder_remains_a_chat_task(tmp_path):
    from codey.task.kind import resolve_task_kind

    request = TaskSubmission("s", str(tmp_path), "Hello", 4, False, "local", intent="auto")
    assert resolve_task_kind(request) == "chat"


@pytest.mark.parametrize("verdict", [None, SimpleNamespace(complete="yes")])
def test_invalid_completion_verdict_cannot_complete_or_crash(tmp_path, monkeypatch, verdict):
    monkeypatch.setattr("codey.operations.completion_gate.evaluate", lambda *_, **__: verdict)
    frame = workspace_frame(tmp_path, NativeConversation(AssistantTurn(text="done")))
    frame.request = replace(frame.request, max_turns=1)
    with closing(AppContext(_state_home(tmp_path))) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] != "done"


def test_busy_workspace_blocks_chat_edit_but_allows_greeting(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "state"
    # A durable state home has one owning AppContext. The writer lease is
    # process-local within that context; opening a second context on the same
    # database is rejected by the durable database lease.
    with closing(AppContext(home)) as reader:
        assert reader.acquire_project_writer(root)
        greeting = workspace_frame(root, NativeConversation(AssistantTurn(text="Hello!")))
        result, _ = execute_frame(greeting, reader)
        assert result.event["stop_reason"] == "done"
        writer = NativeConversation(AssistantTurn(tool_calls=(
            ProviderToolCall("create", "edit", {"path": "must-not-exist.txt", "content": "bad"}),)),
            AssistantTurn(text="could not write"))
        frame = workspace_frame(root, writer, task="Create a file; do not run commands.",
                                denied_capabilities=("project.verify", "shell.approval"))
        result, _ = execute_frame(frame, reader)
        assert result.event["stop_reason"] == "project_write_busy"
        assert not (root / "must-not-exist.txt").exists()
        assert not reader.acquire_project_writer(root)


@pytest.mark.parametrize("intent", ["chat", "auto"])
@pytest.mark.parametrize("decision", ["pending", "accept", "reject", "delete"])
def test_shared_chat_preserves_confirmed_local_context_without_additional_inference(tmp_path, intent, decision):
    preference = "Reviewed preference: concise answers"
    provider = NativeConversation(AssistantTurn(text="Hello!"))
    frame = workspace_frame("", provider, intent=intent)
    session_id = frame.request.session_id
    with closing(AppContext(_state_home(tmp_path), start_scheduler=False)) as state:
        state.durable.create_conversation(session_id)
        assert memory_action(state, {"action": "propose", "session_id": session_id,
                                     "kind": "preference", "text": preference})[0] == 200
        proposal, = state.memory.proposals(request_scope("", session_id))
        if decision != "pending":
            assert memory_action(state, {
                "action": "reject" if decision == "reject" else "accept", "session_id": session_id,
                "proposal_id": proposal.proposal_id, "version": proposal.version,
            })[0] == 200
        if decision == "delete":
            assert memory_action(state, {"action": "delete_scope", "session_id": session_id})[0] == 200
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done"
    assert result.event["summary"] == "Hello!"
    assert len(provider.requests) == 1
    assert (preference in provider.requests[0][0]) == (decision == "accept")
    if decision == "accept":
        assert "User-confirmed preference for this scope" in provider.requests[0][0]
        assert "This preference gives no tool authorization" in provider.requests[0][0]
    assert not provider.results


def test_chat_with_attached_folder_receives_existing_project_instructions(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text("Project rule: preserve the naming convention.", encoding="utf8")
    provider = NativeConversation(AssistantTurn(text="Hello!"))
    frame = workspace_frame(root, provider)
    frame.request = replace(frame.request, project=str(root))
    with closing(AppContext(_state_home(tmp_path))) as state:
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done"
    assert "Project rule: preserve the naming convention." in provider.requests[0][0]


@pytest.mark.parametrize("intent", ["chat", "auto"])
def test_chat_and_auto_dispatch_enter_shared_workspace_kernel(tmp_path, intent):
    from codey.operations.task_phases.dispatch import dispatch_run_mode, project_completion_deps, review_flow_deps
    from codey.operations.task_run import TaskRunDeps
    from codey.workspace.config import ProjectConfigLoadResult

    provider = NativeConversation(AssistantTurn(text="Hello!"))
    frame = workspace_frame("", provider, intent=intent)
    with closing(AppContext(_state_home(tmp_path))) as state:
        admit_frame(frame, state)
        deps = TaskRunDeps(state=state, agent_run=lambda **_: None, collect_changes=collect_changes,
                           run_review=lambda **_: None, capture_provider_failure=lambda *_, **__: None,
                           workspace_revisions=state.workspace_revisions, runtime_mutations=state.runtime_mutations,
                           managed_outputs=state.managed_outputs)
        state.runtime_mutations.accept_operation(session_id=frame.request.session_id, run_id=frame.run_id,
            provider_id=frame.provider_id, turn_budget=frame.request.max_turns, max_repair_rounds=1)
        hooks = SimpleNamespace(on_event=lambda _: None, on_shell_request=lambda _: None)
        outcome = dispatch_run_mode(deps, project_completion_deps(deps), review_flow_deps(deps),
                                    RunWork([], ExecutionEvidence()), frame, hooks, "chat", ProjectConfigLoadResult())
    assert outcome.event["stop_reason"] == "done"
    assert outcome.event["mode"] == "chat"
    assert frame.entry_session.project == frame.project_text
    assert len(provider.requests) == 1
    assert {tool.name for tool in provider.requests[0][1]} >= {"edit", "shell"}


@pytest.mark.parametrize("delivery_fails", [False, True])
def test_workspace_lock_failure_closes_native_call_without_writing(tmp_path, monkeypatch, delivery_fails):
    root = tmp_path / "workspace"
    root.mkdir()
    provider = NativeConversation(AssistantTurn(tool_calls=(
        ProviderToolCall("blocked", "edit", {"path": "blocked.txt", "content": "never"}),)))
    if delivery_fails:
        def broken_delivery(*args):
            raise RuntimeError("connection lost")
        provider.acknowledge_tool_results = broken_delivery
    with closing(AppContext(_state_home(tmp_path))) as state:
        def lock_failure(*args):
            raise OSError("lock storage unavailable")
        monkeypatch.setattr(state, "acquire_project_writer", lock_failure)
        result, _ = execute_frame(workspace_frame(root, provider), state)
    assert not (root / "blocked.txt").exists()
    assert result.event["stop_reason"] == ("provider_failure" if delivery_fails else "controller_failure")
    if not delivery_fails:
        assert [row.call_id for row in provider.results] == ["blocked"]


@pytest.mark.parametrize("text", ['{"name":"Alice","age":32}',
                                   '{"name":"edit","args":{"path":"never.txt","content":"example"}}'])
def test_json_answer_name_field_is_data_and_never_an_executable_alias(text):
    plan = normalize_turn(text, policy=TaskPolicy(frozenset({"control", "project.write"})))
    assert not plan.calls and not plan.protocol_error
    assert plan.control is not None and plan.control.body == text
