"""A received Auto first turn enters the kernel without preparing it again."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.app.context import AppContext
from codey.operations import auto_loop, project_prompt_context, task_loop
from codey.operations.context import RunWork
from codey.operations.kernel_protocol import InitialNativeTurn, build_turn_snapshot
from codey.operations.task_entry import run_entry_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolResult
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.toolchain import tool_spec
from codey.workspace.changes import collect_changes
from tests.test_auto_direct_answer_continues_to_kernel import _auto_deps, _auto_frame
from tests.test_native_auto_first_turn_preserves_tools_budget_and_authorization import NativeProvider


@pytest.mark.parametrize("swap_registry_after_send", [False, True])
def test_received_auto_turn_prepares_once_and_executes_original_snapshot(tmp_path, monkeypatch, swap_registry_after_send):
    project = tmp_path / "project"
    project.mkdir()
    provider = NativeProvider(AssistantTurn(tool_calls=(
        ProviderToolCall("edit-1", "edit", {"path": "result.txt", "content": "changed"}),
    )))
    bindings = []
    name = "auto_snapshot_probe"
    if swap_registry_after_send:
        assert tool_spec.register_custom_tool(name, grant="control")
        assert tool_spec.register_custom_executor(name, lambda call: bindings.append("original") or ToolResult(call, "old", ok=True))
        provider.first = replace(provider.first, tool_calls=(*provider.first.tool_calls, ProviderToolCall("probe-1", name, {})))
        real_send = provider.send_turn

        def send_and_swap(text, tools):
            turn = real_send(text, tools)
            assert tool_spec.unregister_custom_tool(name)
            assert tool_spec.register_custom_tool(name, grant="project.verify")
            assert tool_spec.register_custom_executor(name, lambda call: bindings.append("replacement") or ToolResult(call, "new", ok=True))
            return turn

        monkeypatch.setattr(provider, "send_turn", send_and_swap)
    frame = _auto_frame("Create result.txt; do not run commands.", provider, project_changes_required=True)
    frame.request = replace(frame.request, project=str(project), max_turns=2)
    frame.project_text = str(project)
    frame.fresh_chat = True
    frame.entry_policy = TaskPolicy(frozenset({"control", "project.read", "project.write"}),
                                   denied_capabilities=frozenset({"project.verify", "shell.approval"}))
    state = AppContext(tmp_path / "state")
    refreshes, executions = [], []
    real_refresh = project_prompt_context.refresh_verification_candidates
    real_execute = task_loop._call_execute_turn

    def refresh(session):
        refreshes.append(session)
        return real_refresh(session)

    def execute(*args, **kwargs):
        executions.append(len(refreshes))
        assert len(provider.requests) == 1
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(project_prompt_context, "refresh_verification_candidates", refresh)
    monkeypatch.setattr(task_loop, "_call_execute_turn", execute)
    runtime = SimpleNamespace(state=state, workspace_revisions=state.workspace_revisions, collect_changes=collect_changes)
    deps = replace(_auto_deps(state, SimpleNamespace()), acquire_writer=lambda _: True, release_writer=lambda _: None,
                   continue_task=lambda f, w, h, **kw: run_entry_kernel(f, w, h, runtime, task_kind="project"))
    try:
        result = auto_loop.run_auto_mode(frame, RunWork([], ExecutionEvidence()),
                                         SimpleNamespace(on_event=lambda _: None, on_shell_request=None), deps)
        assert result.event["stop_reason"] == "done"
        assert result.event["turns"] == 2
        assert executions == [1]
        assert provider.resets == 1
        assert (project / "result.txt").read_text(encoding="utf8") == "changed"
        assert [r.call_id for r in provider.results] == (["edit-1", "probe-1", "done-2"] if swap_registry_after_send else ["edit-1", "done-2"])
        assert bindings == (["original"] if swap_registry_after_send else [])
    finally:
        state.close()
        if swap_registry_after_send:
            tool_spec.unregister_custom_tool(name)


def test_initial_turn_from_another_session_is_rejected_even_with_equal_policy(tmp_path):
    policy = TaskPolicy(frozenset({"control", "project.read"}))
    original = TaskSession(policy=policy, task_kind="planning_readonly", project=str(tmp_path / "original"))
    other = TaskSession(policy=policy, task_kind="planning_readonly", project=str(tmp_path), max_turns=1)
    (tmp_path / "private.txt").write_text("belongs to another task", encoding="utf8")
    first = AssistantTurn(tool_calls=(ProviderToolCall("read-1", "read_file", {"path": "private.txt"}),))
    initial = InitialNativeTurn(first, build_turn_snapshot(original, native=True), "Read the original project", owner_session=original)
    provider = NativeProvider(first)
    result = task_loop.run_task_kernel(other, request=task_loop.KernelRunRequest(
        transport=task_loop.KernelTransportDeps(provider=provider, initial_turn=initial),
        execution=task_loop.KernelExecutionDeps(project_path=tmp_path),
    ))
    assert result.stop_reason == "controller_failure"
    assert not other.read_files
    assert not provider.results and not provider.requests
