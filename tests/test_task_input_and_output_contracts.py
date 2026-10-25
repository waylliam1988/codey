from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolCall


def test_prepared_requests_keep_history_prefix_and_count_exact_sent_payload(monkeypatch):
    from codey.providers import api_transport
    from codey.providers.token_accounting import RequestContextCount

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    clocks = iter(["clock-1", "clock-2"])
    monkeypatch.setattr("codey.providers.request_context.clock_context", lambda: next(clocks))
    counted, sent = [], []

    def count(payload, *, deadline):
        counted.append(json.loads(json.dumps(payload)))
        return RequestContextCount(20, "tokenizer")

    def opened(request, timeout):
        sent.append(json.loads(request.data))
        return Response(b'{"choices":[{"message":{"content":"answer"},"finish_reason":"stop"}]}')

    monkeypatch.setattr(api_transport, "open_request", opened)
    provider = ApiProvider("http://localhost:9/v1", "fixture", system_prompt="stable rules", request_counter=count)
    provider.send("first observation")
    provider.send("next observation")
    assert counted == sent
    assert sent[0]["messages"][:-1] == sent[1]["messages"][:2]
    assert sent[1]["messages"][2:] == [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next observation"},
        {"role": "user", "content": "clock-2"},
    ]
    assert not hasattr(provider, "_working_context")


@pytest.mark.parametrize("answer", ['```json\n{"x":1}\n```', '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '{"x":"1"}'])
def test_json_requirement_survives_policy_restore_and_rejects_invalid_answers(answer):
    schema = json.dumps({"type": "object", "required": ["x"], "additionalProperties": False,
                         "properties": {"x": {"type": "integer"}}})
    policy = build_task_policy(SimpleNamespace(project="", output_format="json", output_schema=schema), task_kind="chat")
    restored = TaskPolicy.from_payload(policy.to_payload())
    assert restored == policy
    session = TaskSession(policy=restored, task_kind="chat")
    verdict = evaluate(session, answer)
    assert not verdict.complete
    assert "Repair only the final answer" in verdict.followup
    assert evaluate(session, '{"x":1}').complete


@pytest.mark.parametrize("native", [False, True])
def test_format_repair_uses_original_loop_without_repeating_read(tmp_path, monkeypatch, native):
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **kw: native)
    (tmp_path / "a.txt").write_text("observed\n")
    calls = [("read_file", {"path": "a.txt"}), ("done", {"summary": '```json\n{"x":1}\n```'}),
             ("done", {"summary": '{"x":1}'})]
    if native:
        calls.insert(0, ("read_file", {"args": {"path": "a.txt"}}))

    class Provider:
        name = "Local"

        def __init__(self):
            self.prompts = []
            self.results = []
            self.calls = iter(enumerate(calls))

        def reply(self):
            i, (name, args) = next(self.calls)
            return (AssistantTurn(tool_calls=(ProviderToolCall(f"call-{i}", name, args),)) if native else
                    json.dumps({"tool": name, "args": args}))

        def send(self, prompt, timeout=None):
            self.prompts.append(prompt)
            return self.reply()

        def send_turn(self, prompt, tools, timeout=None):
            return self.send(prompt)

        def send_tool_results(self, results, tools, timeout=None):
            self.results.extend(results)
            return self.reply()

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return AssistantTurn()

    provider = Provider()
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"}), required_checks=(declaration("json"),)),
                          task_kind="project", project=str(tmp_path), max_turns=5)
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=provider, provider_id="local", run_id="format", user_task="Read a.txt"),
        execution=KernelExecutionDeps(project_path=tmp_path)))
    assert result.completed and result.summary == '{"x":1}'
    assert len([r for r in session.executed.values() if r["name"] == "read_file"]) == 1
    if native:
        assert "omit the text protocol's tool/args envelope" in provider.prompts[0]
        assert "missing required arg" in provider.results[0].content
        assert "omit the text protocol's tool/args envelope" in provider.results[0].user_content
        assert "Current work observations" in provider.results[1].user_content
        assert "Repair only the final answer" in provider.results[-1].content
    else:
        assert "Current work observations" in provider.prompts[0]


def test_undeclared_unknown_metadata_is_still_rejected():
    from codey.toolchain.tool_args_repair import ToolArgsRepairError, normalize_tool_args

    with pytest.raises(ToolArgsRepairError, match="unsupported fields"):
        normalize_tool_args("edit", {"path": "a", "content": "x", "thought": "ignored?"})


@pytest.mark.parametrize("format,schema", [("yaml", ""), ("text", '{"type":"object"}'),
                                          ("json", '{"type":"object","type":"array"}')])
def test_invalid_output_contract_is_rejected_before_task_admission(format, schema):
    from unittest.mock import Mock

    from codey.app.task_submit import submit_task

    state = Mock(side_effect=AssertionError("invalid contract reached application state"))
    with pytest.raises(ValueError):
        submit_task("s", None, "answer", 4, False, "deepseek", get_state=state,
                    output_format=format, output_schema=schema)
    state.assert_not_called()


def test_api_passes_explicit_contract_and_rejects_invalid_contract(monkeypatch):
    from unittest.mock import Mock

    from codey.app import api

    monkeypatch.setattr(api, "_model_disabled", lambda *args: False)
    submit = Mock(return_value="accepted")
    schema = '{"type":"object","required":["x"],"properties":{"x":{"type":"integer"}}}'
    body = {"task": "return result", "provider": "deepseek", "output_format": "json", "output_schema": schema}
    assert api.run_submit_response(body, submit)[0] == 200
    assert submit.call_args.kwargs["output_schema"] == schema
    assert submit.call_args.kwargs["output_format"] == "json"
    submit.reset_mock()
    assert api.run_submit_response({**body, "output_schema": '{"type":"unknown"}'}, submit)[0] == 400
    submit.assert_not_called()


def test_durable_dispatch_restores_same_admitted_output_contract(tmp_path, monkeypatch):
    from contextlib import closing
    from unittest.mock import Mock

    from codey.app import task_submit
    from codey.app.context import AppContext
    from codey.task.model import StartTask

    schema = '{"type":"object","required":["x"],"properties":{"x":{"type":"integer"}}}'
    worker = Mock(return_value=True)
    monkeypatch.setattr(task_submit, "submit_browser_task", worker)
    home = tmp_path / "state"
    with closing(AppContext(home, start_scheduler=False)) as state:
        assert task_submit.submit_task("s", None, "answer", 4, False, "deepseek", run_id="json-task",
            input=StartTask("answer"), get_state=lambda: state, output_format="json", output_schema=schema) == "json-task"
        assert worker.call_args.kwargs["output_schema"] == schema
        state.durable.unclaim_task("json-task")
        state.release_run("json-task")
    worker.reset_mock()
    with closing(AppContext(home, start_scheduler=False)) as state:
        admitted = state.durable.task("json-task")
        assert admitted.state["output_format"] == "json"
        assert admitted.state["output_schema"] == schema
        assert task_submit.dispatch_durable_task(admitted, state=state)
        assert worker.call_args.kwargs["output_format"] == "json"
        assert worker.call_args.kwargs["output_schema"] == schema
