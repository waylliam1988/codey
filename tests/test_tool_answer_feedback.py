"""One committed observation drives both the verdict and actionable feedback."""
import json
from types import SimpleNamespace

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.kernel_prompt import _format_results, _result_context
from codey.operations.kernel_transport import _native_tool_results
from codey.policies.tool_requirement import require_tool_success
from tests.test_required_tool_lineage import _code, _execute, _restart
from tests.test_required_tool_lineage import runtime as runtime

BINDING = (require_tool_success("read_file", via="codemode", arguments={"path": "a.txt"},
                               answer_pointer="/text", result_pointer="/text", result_line=1),)


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
@pytest.mark.parametrize("answer,reason", [
    ('{"text":"\\"PATTERN.*\\""}', "answer_value_mismatch"),
    ('{"text":true}', "answer_type_mismatch"),
    ('{}', "answer_pointer_missing"),
    ('not JSON', "answer_json_invalid"),
    ('{"text":"wrong","text":"PATTERN.*"}', "answer_json_invalid"),
    ('{"text":NaN}', "answer_json_invalid"),
    ('{"text":1e999}', "answer_json_invalid"),
    ('```json\n{"text":"PATTERN.*"}\n```', "answer_json_invalid"),
])
def test_valid_receipt_only_requests_answer_repair_and_survives_restart(runtime, answer, reason):
    (runtime.project / "a.txt").write_text("PATTERN.*\n", encoding="utf-8")
    _execute(runtime, [_code("return (await tools.read_file({path:'a.txt'})).value.text;")], True)
    verdict = evaluate(runtime.state, answer)
    assert not verdict.complete and reason in verdict.followup
    assert "Execution and observation are already satisfied" in verdict.followup
    assert "Required execution:" not in verdict.followup
    assert "Do not repeat" in verdict.followup
    assert "without Markdown fences" in verdict.followup
    assert 'Expected selected value (JSON): "PATTERN.*"' in verdict.followup
    assert "task_turn_effect:" in verdict.followup
    assert evaluate(_restart(runtime), answer).followup == verdict.followup


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
def test_missing_execution_and_missing_receipt_have_distinct_feedback(runtime):
    missing = evaluate(runtime.state, '{"text":"PATTERN.*"}')
    assert "tool_execution_missing" in missing.followup and "Required execution:" in missing.followup
    (runtime.project / "a.txt").write_text("PATTERN.*\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    runtime.state._memory_results.clear()
    verdict = evaluate(runtime.state, '{"text":"PATTERN.*"}')
    assert "observation_receipt_invalid" in verdict.followup
    assert "already satisfied" not in verdict.followup


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
def test_feedback_and_verdict_never_reconstruct_observations_from_display(runtime):
    from dataclasses import replace

    (runtime.project / "a.txt").write_text("PATTERN.*\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    identity = next(key for key, row in runtime.state.executed.items() if row["name"] == "read_file")
    receipt = runtime.state._memory_results[identity]
    runtime.state._memory_results[identity] = replace(receipt, model_text="display decoy")
    assert evaluate(runtime.state, '{"text":"PATTERN.*"}').complete
    verdict = evaluate(runtime.state, '{"text":"display decoy"}')
    assert 'Expected selected value (JSON): "PATTERN.*"' in verdict.followup
    runtime.state._memory_results[identity] = replace(receipt, canonical={})
    verdict = evaluate(runtime.state, '{"text":"PATTERN.*"}')
    assert not verdict.complete and "observation_receipt_invalid" in verdict.followup


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
@pytest.mark.parametrize("parent_failed", [False, True])
def test_successful_leaf_cannot_replace_the_required_successful_parent(runtime, parent_failed):
    (runtime.project / "a.txt").write_text("PATTERN.*\n", encoding="utf-8")
    call = (_code("await tools.read_file({path:'a.txt'}); throw new Error('after child');") if parent_failed else
            {"tool": "read_file", "args": {"path": "a.txt"}})
    _execute(runtime, [call], True)
    verdict = evaluate(runtime.state, '{"text":"PATTERN.*"}')
    assert "tool_parent_missing" in verdict.followup and "Required execution:" in verdict.followup
    assert not verdict.complete


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
def test_conflicts_and_unobserved_lines_do_not_request_answer_only_repair(runtime):
    (runtime.project / "a.txt").write_text("first\nsecond\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt',offset:2});")], True)
    assert "observation_selector_unavailable" in evaluate(runtime.state, '{"text":"first"}').followup
    for turn, text in enumerate(("before", "after"), 2):
        (runtime.project / "a.txt").write_text(text + "\n", encoding="utf-8")
        _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True, start=turn)
    verdict = evaluate(runtime.state, '{"text":"before"}')
    assert "conflicting_observations" in verdict.followup
    assert "already satisfied" not in verdict.followup


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
def test_long_feedback_is_bounded_and_never_presents_a_preview_as_the_complete_value(runtime):
    from codey.operations.tool_result_reader import read_tool_result
    from codey.runtime.core.models import ToolCall

    (runtime.project / "a.txt").write_text("中文\\\"" * 3000 + "\n", encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    verdict = evaluate(runtime.state, json.dumps({"text": "z" * 9000}))
    assert not verdict.complete
    assert len(verdict.followup) < 5000
    assert '"truncated":true' in verdict.followup and "Do not copy a truncated preview" in verdict.followup
    assert 'view="value"' in verdict.followup
    identity = next(key for key, row in runtime.state.executed.items() if row["name"] == "read_file")
    restored = _restart(runtime)
    delegate = SimpleNamespace(session=restored)
    chunks, offset = [], 0
    while True:
        page = read_tool_result(delegate, ToolCall("read_tool_result", {"result_ref": identity,
            "view": "value", "offset": offset, "limit": 8000}))
        assert page.ok
        data = json.loads(page.model_text)
        chunks.append(data["text"])
        if data["next_offset"] is None:
            break
        offset = data["next_offset"]
    assert json.loads("".join(chunks))["value"] == restored._memory_results[identity].canonical["value"]


@pytest.mark.parametrize("runtime", [None], indirect=True)
@pytest.mark.parametrize("value", ["PATTERN.*", '"PATTERN.*"', "", None, False, 0, [], {"a": "\\\"中"}])
def test_codemode_typed_value_has_identical_native_text_and_recovered_views(runtime, value):
    result, = _execute(runtime, [_code("return " + json.dumps(value, ensure_ascii=False) + ";")], True)
    assert result.canonical["value"] == value
    assert json.loads(result.model_text) == {"ok": True, "value": value}
    view = _result_context(result, runtime.state)
    assert "Result data (JSON):\n" in view and "\n\nResult metadata:\n" in view
    assert _native_tool_results([result], runtime.state)[0].content == view
    assert view in _format_results([result], runtime.state)
    restored = _restart(runtime)
    recovered = next(row for row in restored._memory_results.values() if row.call.name == "codemode")
    assert _result_context(recovered, restored) == view


@pytest.mark.parametrize("runtime", [BINDING], indirect=True)
def test_legitimate_content_quotes_are_never_stripped(runtime):
    (runtime.project / "a.txt").write_text('"PATTERN.*"\n', encoding="utf-8")
    _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")], True)
    assert evaluate(runtime.state, json.dumps({"text": '"PATTERN.*"'})).complete
    assert not evaluate(runtime.state, json.dumps({"text": "PATTERN.*"})).complete
    feedback = evaluate(runtime.state, json.dumps({"text": "PATTERN.*"})).followup
    selected = feedback.split("Expected selected value (JSON): ")[1].splitlines()[0]
    assert json.loads(selected) == '"PATTERN.*"'
    actual = feedback.split("Actual answer value (JSON): ")[1].splitlines()[0]
    assert json.loads(actual) == "PATTERN.*"
