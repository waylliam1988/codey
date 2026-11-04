"""Muse through optional Zen receives the shared workspace snapshot and approvals."""
from contextlib import closing
from dataclasses import replace

import pytest

from codey.app.context import AppContext
from tests.support.workspace_conversation import short_test_state_home
from tests.test_chat_workspace_tools_and_answer_completion import execute_frame, workspace_frame
from tests.test_zen_transport_declarations_never_grant_execution import provider_with_responses, response


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_strict_research_forwards_tool_requirement_through_zen_native_continuation(tmp_path, monkeypatch, protocol):
    from tests.support.kernel_runtime import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
    from tests.test_research_findings import URL, setup

    provider, requests = provider_with_responses(monkeypatch, [
        response(calls=[("read-source", "open_url", {"url": URL})]),
        response(calls=[("submit-answer", "submit_research_answer", {
            "state": "answered", "markdown": "Only one writer can write at a time [e1]."})]),
    ], protocol, expected_tool_choice="required")
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = replace(session.policy, strict_research=True)
        session.max_turns = 2
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, user_task=session.task_text),
            execution=KernelExecutionDeps(research_tools=tools)))
        assert result.completed and result.stop_reason == "done", result.summary
        assert result.proof.satisfied and result.research_report["schema"] == "research_answer_v1"
        assert "[e1]" in result.summary and tools.ledger.read_passages
        assert len(requests) == 2
        if protocol == "openai-responses":
            calls = [row for row in requests[1]["input"] if row.get("type") == "function_call"]
            outputs = [row for row in requests[1]["input"] if row.get("type") == "function_call_output"]
            assert calls[0]["call_id"] == outputs[0]["call_id"] == "read-source"
        else:
            calls = [call for row in requests[1]["messages"] for call in row.get("tool_calls", [])]
            outputs = [row for row in requests[1]["messages"] if row.get("role") == "tool"]
            assert calls[0]["id"] == outputs[0]["tool_call_id"] == "read-source"
    finally:
        provider.close()
        store.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("case", ["answer", "edit", "shell"])
def test_muse_chat_uses_shared_workspace_tools_without_provider_core_branch(tmp_path, monkeypatch, protocol, case):
    replies = ([response("Hello")] if case == "answer" else
               [response(calls=[("create", "edit", {"path": "result.txt", "content": "actual"})]),
                response("Created result.txt")] if case == "edit" else
               [response(calls=[("approve", "shell", {"command": "python --version", "path": "."})])])
    provider, requests = provider_with_responses(monkeypatch, replies, protocol)
    provider.runtime.model = "muse-spark-1.3"
    frame = workspace_frame(tmp_path, provider, task="Hello" if case == "answer" else "Do the requested action")
    if case == "edit":
        from dataclasses import replace
        frame.request = replace(frame.request, denied_capabilities=("project.verify",))
    with closing(AppContext(short_test_state_home())) as state:
        result, events = execute_frame(frame, state)
    assert result.event["stop_reason"] == ("approval" if case == "shell" else "done")
    assert len(requests) == (2 if case == "edit" else 1)
    declarations = [tool.get("function", tool) for tool in requests[0]["tools"]]
    shell = next(tool for tool in declarations if tool["name"] == "shell")
    assert "unavailable" not in shell["description"].lower()
    assert all(request["model"] == "muse-spark-1.3" for request in requests)
    if case == "edit":
        assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "actual"
    if case == "shell":
        assert any(getattr(event, "command", "") == "python --version" for event in events)
