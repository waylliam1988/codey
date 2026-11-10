"""Receipt-bound output uses the ordinary task driver and original observations."""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from codey.app.api import run_submit_response
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import OutputBindingError, prepare_completion_output
from codey.policies.output_requirement import declaration
from codey.policies.tool_requirement import require_tool_success
from tests.test_required_tool_lineage import _code, _execute, _restart
from tests.test_required_tool_lineage import runtime as runtime


def _binding(path, result_pointer="/text", *, file="a.txt", line=None):
    return require_tool_success("read_file", via="codemode", arguments={"path": file},
                                answer_pointer=path, result_pointer=result_pointer, result_line=line)


def _output(state, text="{}"):
    return prepare_completion_output(state, text, CompletionContext())


def test_existing_submission_field_accepts_canonical_bindings_without_granting_permissions():
    from types import SimpleNamespace

    from codey.policies.task_policy import build_task_policy

    binding = _binding("/line", line=1)
    submit = Mock(return_value="run")
    status, _ = run_submit_response({"task": "read", "required_tools": [binding, binding]}, submit)
    assert status == 200
    assert submit.call_args.kwargs["required_tools"] == (binding,)
    policy = build_task_policy(SimpleNamespace(project=None, required_tools=(binding,)), task_kind="chat")
    assert policy.required_checks == (binding,)
    assert not policy.allows("project.read")


@pytest.mark.usefixtures("no_external_advisor_models")
@pytest.mark.parametrize("mode", ["chat", "hybrid"])
def test_headless_entry_delivers_receipt_fields_with_one_submission_and_preserves_model_judgement(tmp_path, mode):
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from tests.test_headless_runner import _RouteProvider

    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text('observed "code"\nsecond\n', encoding="utf-8")
    provider = _RouteProvider([
        json.dumps(_code("return await tools.read_file({path:'a.txt'});")),
        json.dumps({"tool": "done", "args": {"summary": '{"interpretation":"model judgement","lines":-1}'}}),
    ])
    rows = []
    schema = {"type": "object", "properties": {
        "lines": {"type": "integer"}, "code": {"type": "string"}, "interpretation": {"type": "string"}},
        "required": ["lines", "code", "interpretation"], "additionalProperties": False}
    result = run_headless(HeadlessRequest(project=project, task="Read a.txt and explain the result.",
        intent=mode, provider_id="qwen", max_turns=3, project_changes_required=False,
        state_home=tmp_path / "state", research_store_root=tmp_path / "knowledge", output_format="json",
        output_schema=json.dumps(schema), required_tools=(_binding("/lines", "/total_lines"),
                                                         _binding("/code", line=1))),
        emit_jsonl=rows.append, connect_provider=lambda *_a, **_kw: provider)
    assert result.exit_code == 0 and result.stop_reason == "done", rows[-2:]
    final = next(row for row in reversed(rows) if row["type"] == "task_done")
    assert json.loads(final["summary"]) == {"lines": 2, "code": 'observed "code"', "interpretation": "model judgement"}
    assert len(provider.prompts) == 2
    assert sum(row["type"] == "tool" and row["tool_name"] == "read_file" for row in rows) == 1


TYPED = tuple(_binding(f"/{key}", pointer) for key, pointer in (
    ("text", "/text"), ("lines", "/total_lines"), ("complete", "/text_complete")))


@pytest.mark.parametrize("runtime", [TYPED], indirect=True)
def test_original_receipts_supply_typed_fields_and_preserve_unbound_content_and_recovery(runtime, monkeypatch):
    (runtime.project / "a.txt").write_text('中文 "quotes"\nsecond\n', encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    original = dict(runtime.state._memory_results)
    proposed = '{"lines":"wrong","text":"short","complete":0,"explanation":"model judgement"}'
    output = _output(runtime.state, proposed)
    expected = {"text": '中文 "quotes"\nsecond\n', "lines": 2, "complete": True, "explanation": "model judgement"}
    assert json.loads(output.text) == expected
    with monkeypatch.context() as checks:
        checks.setattr("codey.policies.tool_requirement.ToolSuccessRequirement.observe",
                       lambda *_a, **_kw: pytest.fail("Gate repeated receipt selection"))
        assert evaluate(runtime.state, output.text, observations=output.observations).complete
    assert runtime.state._memory_results == original
    fresh = _restart(runtime)
    before = runtime.mutations.durable.task_facts("r")
    (runtime.project / "a.txt").write_text("changed later\n", encoding="utf-8")
    monkeypatch.setattr("codey.toolchain.runtime.read_file", lambda *_a, **_kw: pytest.fail("Repeated read"))
    assert _output(fresh, proposed) == output
    assert runtime.mutations.durable.task_facts("r") == before


@pytest.mark.parametrize("runtime", [(_binding(""),)], indirect=True)
def test_entire_root_value_can_be_a_string_without_json_string_damage(runtime):
    text = '中文 "quoted"\n' + "x" * 400
    (runtime.project / "a.txt").write_text(text, encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], False)
    output = _output(runtime.state, "No copying needed")
    assert json.loads(output.text) == text
    assert evaluate(runtime.state, output.text, observations=output.observations).complete


@pytest.mark.parametrize("runtime", [(declaration("json", json.dumps({"type": "object", "properties": {
    "a/b": {"type": "object", "properties": {"~key": {"type": "array", "items": {"type": "integer"}}}}}})),
    _binding("/a~1b/~0key/0", "/total_lines"))], indirect=True)
def test_schema_determines_nested_array_shape_and_escaped_property_names(runtime):
    (runtime.project / "a.txt").write_text("first\nsecond\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    output = _output(runtime.state)
    assert json.loads(output.text) == {"a/b": {"~key": [2]}}
    assert evaluate(runtime.state, output.text, observations=output.observations).complete


@pytest.mark.parametrize("runtime", [(declaration("json", '{"type":"array","items":{"type":"integer"}}'),
                                     _binding("/1", "/total_lines"))], indirect=True)
def test_sparse_array_requests_unbound_content_without_inventing_placeholder_values(runtime):
    (runtime.project / "a.txt").write_text("one\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    with pytest.raises(ValueError, match="unbound preceding") as failure:
        _output(runtime.state)
    assert not isinstance(failure.value, OutputBindingError)
    output = _output(runtime.state, "[7]")
    assert json.loads(output.text) == [7, 1]
    assert evaluate(runtime.state, output.text, observations=output.observations).complete


@pytest.mark.parametrize("runtime", [(_binding("/text"),)], indirect=True)
def test_missing_receipt_and_conflicting_reads_cannot_be_filled_from_model_claims(runtime):
    for turn, text in enumerate(("before", "after"), 1):
        (runtime.project / "a.txt").write_text(text, encoding="utf-8")
        _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True, start=turn)
    fresh = _restart(runtime)
    proposed = '{"text":"after"}'
    output = _output(fresh, proposed)
    assert output.text == proposed
    assert "conflicting_observations" in evaluate(fresh, output.text, observations=output.observations).followup
    fresh._memory_results.clear()
    output = _output(fresh, proposed)
    assert "observation_receipt_invalid" in evaluate(fresh, output.text, observations=output.observations).followup


@pytest.mark.parametrize("runtime", [(_binding("/x", "/total_lines"), _binding("/x", "/text_complete"))], indirect=True)
def test_conflicting_destination_values_are_a_contract_error(runtime):
    (runtime.project / "a.txt").write_text("one\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    with pytest.raises(OutputBindingError, match="same JSON field"):
        _output(runtime.state)


@pytest.mark.parametrize("runtime", [(_binding("/result", ""), _binding("/result/total_lines", "/total_lines"))], indirect=True)
def test_consistent_overlapping_bindings_preserve_original_receipts(runtime):
    (runtime.project / "a.txt").write_text("one\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    original = dict(runtime.state._memory_results)
    output = _output(runtime.state)
    assert json.loads(output.text)["result"]["total_lines"] == 1
    assert evaluate(runtime.state, output.text, observations=output.observations).complete
    assert runtime.state._memory_results == original
    runtime.state.policy = replace(runtime.state.policy, required_checks=(runtime.state.policy.required_checks[0],
        _binding("/result/total_lines", "/text_complete")))
    with pytest.raises(OutputBindingError, match="Overlapping"):
        _output(runtime.state)
    assert runtime.state._memory_results == original


@pytest.mark.parametrize("runtime", [None], indirect=True)
def test_ordinary_text_has_no_projection(runtime):
    text = "Model interpretation, preserved verbatim."
    assert _output(runtime.state, text).text == text
