"""Native auto must hand its first authorized turn to the existing kernel."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from codey.app.context import AppContext
from codey.operations.auto_loop import run_auto_mode
from codey.operations.context import RunWork
from codey.operations.task_entry import run_entry_kernel
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.changes import collect_changes
from tests.test_auto_direct_answer_continues_to_kernel import _auto_deps, _auto_frame


class NativeProvider:
    native_tools = True

    def __init__(self, first):
        self.first = first
        self.requests = []
        self.results = []
        self.resets = 0

    def new_chat(self):
        self.resets += 1

    def send(self, text):
        raise AssertionError("native auto must not send an untyped routing request")

    def send_turn(self, text, tools):
        self.requests.append((text, tools))
        return self.first

    def send_tool_results(self, results, tools):
        self.results.extend(results)
        return AssistantTurn(tool_calls=(ProviderToolCall("done-2", "done", {"summary": "finished"}),))

    def acknowledge_tool_results(self, results, declared_tools, timeout=None):
        self.results.extend(results)
        return "delivered"


def test_first_native_edit_executes_once_in_original_budget_and_fresh_window(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    provider = NativeProvider(AssistantTurn(tool_calls=(
        ProviderToolCall("edit-1", "edit", {"path": "result.txt", "content": "changed"}),
    )))
    frame = _auto_frame("Create result.txt with changed; do not run commands.", provider,
                        project_changes_required=True)
    frame.request = replace(frame.request, project=str(project), max_turns=2)
    frame.project_text = str(project)
    frame.fresh_chat = True
    frame.entry_policy = TaskPolicy(frozenset({"control", "project.read", "project.write"}),
                                    denied_capabilities=frozenset({"project.verify", "shell.approval"}))
    state = AppContext(tmp_path / "state")
    hooks = SimpleNamespace(on_event=lambda _: None, on_shell_request=None)
    work = RunWork([], ExecutionEvidence())
    runtime = SimpleNamespace(state=state, workspace_revisions=state.workspace_revisions,
                              collect_changes=collect_changes)
    acquired = []

    def continuation(active_frame, active_work, active_hooks, *, followup):
        assert len(provider.requests) == 1
        assert not (project / "result.txt").exists()
        acquired.append("execute")
        return run_entry_kernel(active_frame, active_work, active_hooks, runtime,
                                task_kind="project", continuation_followup=followup)

    deps = replace(_auto_deps(state, SimpleNamespace()), continue_task=continuation,
                   acquire_writer=lambda _: acquired.append("lease") or True,
                   release_writer=lambda _: acquired.append("release"))
    try:
        result = run_auto_mode(frame, work, hooks, deps)
        assert result.event["stop_reason"] == "done"
        assert result.event["turns"] == 2
        assert (project / "result.txt").read_text(encoding="utf-8") == "changed"
        assert provider.resets == 1
        assert len(provider.requests) == 1
        assert [r.call_id for r in provider.results] == ["edit-1", "done-2"]
        names = {tool.name for tool in provider.requests[0][1]}
        assert "edit" in names and "run" not in names
        assert "ACTION:" not in provider.requests[0][0]
        assert acquired == ["lease", "execute", "release"]
    finally:
        state.close()


def test_native_greeting_keeps_single_turn_completion_and_does_not_take_writer():
    provider = NativeProvider(AssistantTurn(text="Hello!"))
    frame = _auto_frame("hello", provider)
    lease = Mock()
    continuation = Mock()
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()),
                   acquire_writer=lease, continue_task=continuation)
    result = run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(on_event=lambda _: None), deps)
    assert result.event["stop_reason"] == "done"
    assert len(provider.requests) == 1
    lease.assert_not_called()
    continuation.assert_not_called()


def test_incomplete_native_text_is_not_accepted_as_complete_answer():
    provider = NativeProvider(AssistantTurn(text="Hello!", finish=TurnFinish.OUTPUT_LIMIT))
    frame = _auto_frame("hello", provider)
    continuation = Mock(return_value=SimpleNamespace(event={"stop_reason": "max_turns"}))
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()), continue_task=continuation)
    result = run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(on_event=lambda _: None), deps)
    assert result.event["stop_reason"] == "max_turns"
    continuation.assert_called_once()
    assert frame.entry_initial_turn.reply.finish is TurnFinish.OUTPUT_LIMIT


def test_busy_writer_closes_received_call_without_executing_or_dropping_it():
    provider = NativeProvider(AssistantTurn(tool_calls=(
        ProviderToolCall("edit-1", "edit", {"path": "result.txt", "content": "changed"}),
    )))
    frame = _auto_frame("Create result.txt.", provider, project_changes_required=True)
    continuation = Mock()
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()),
                   acquire_writer=lambda _: False, continue_task=continuation)
    result = run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(on_event=lambda _: None), deps)
    assert result.event["stop_reason"] == "stopped"
    continuation.assert_not_called()
    assert [r.call_id for r in provider.results] == ["edit-1"]
    assert "not executed" in provider.results[0].content
    assert frame.entry_initial_turn is None
