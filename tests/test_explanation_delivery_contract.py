"""Explicit explanation obligations reuse receipt selection and output validation."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import OutputBindingError, prepare_completion_output
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.output_requirement import evaluate as evaluate_output
from codey.policies.tool_requirement import (
    ToolSuccessRequirement,
    argument_digest,
    parse_tool_success_check,
    require_tool_success,
)
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, parse_json_object

PARAGRAPH = r"[^\S\r\n]*\S[^\r\n]*(?:\r?\n[^\S\r\n]*\S[^\r\n]*)*"
SEPARATOR = r"(?:\r?\n[^\S\r\n]*){2,}"
TWO_PARAGRAPHS = rf"\A{PARAGRAPH}{SEPARATOR}{PARAGRAPH}\Z"
AT_LEAST_TWO = rf"\A{PARAGRAPH}(?:{SEPARATOR}{PARAGRAPH})+\Z"


def explanation_schema(pattern: str = "") -> JsonObject:
    text: JsonObject = {"type": "string", "minLength": 1}
    if pattern:
        text["pattern"] = pattern
    return {"type": "object", "properties": {"explanation": text},
            "required": ["explanation"], "additionalProperties": False}


def requirement() -> ToolSuccessRequirement:
    return ToolSuccessRequirement("read_file", answer_pointer="/explanation", result_pointer="/fact",
                                  answer_contains=True)


def commit(session: TaskSession, value: JsonValue, identity: str = "read-1") -> None:
    call = ToolCall("read_file", {"path": "result.json"}, identity)
    session.executed[identity] = ExecutionRecord(name=call.name, ok=True, call_id=call.call_id, excerpt="",
        args_digest=argument_digest(call.args),
        argument_digests={key: argument_digest(item) for key, item in call.args.items()})
    session._memory_results[identity] = ToolResult(call, "observed", ok=True, canonical={"value": {"fact": value}})


def session_for(value: JsonValue, *, pattern: str = "") -> TaskSession:
    from codey.policies.task_policy import TaskPolicy

    checks = (declaration("json_explanation", json.dumps(explanation_schema(pattern))), requirement().declaration)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"}), required_checks=checks),
                          task_kind="chat", task_text="Report the selected result.")
    commit(session, value)
    return session


@pytest.mark.parametrize(("value", "text", "passed"), [
    (173, "净重173克。", True), (173, "净重1173克。", False), (173, "净重1730克。", False),
    (173, "净重173.5克。", False), (173, "净重-173克。", False), (173, "净重173e2克。", False),
    (173, "ID_173", False), (-2.5, "结果为-2.5。", True), (-2.5, "结果为-2.50。", False),
    ("A_0017", "结果：A_0017。", True), ("A_0017", "结果：XA_0017。", False),
    ("A_0017", "结果：A_00170。", False), ("中文结果", "观察到中文结果。", True),
])
def test_selected_text_and_numbers_must_be_delivered_whole(value: JsonValue, text: str, passed: bool) -> None:
    session = session_for(value)
    output = prepare_completion_output(session, text, CompletionContext())
    rule = requirement()
    outcome = rule.evaluate_observation(output.observations[rule.check_id], output.text)
    assert outcome.passed is passed
    assert parse_json_object(output.text)["explanation"] == text
    if not passed:
        assert outcome.reason_code == "answer_text_missing"
        assert text in outcome.feedback and '"read-1"' in outcome.feedback
        assert "Do not repeat" in outcome.feedback and "done.summary" in outcome.feedback


def test_changed_receipt_rejects_the_old_answer_without_rewriting_it() -> None:
    old = prepare_completion_output(session_for("OLD_17"), "结果是 OLD_17。", CompletionContext())
    current = session_for("NEW_29")
    output = prepare_completion_output(current, "结果是 OLD_17。", CompletionContext())
    assert output.text == old.text
    verdict = evaluate(current, output.text, observations=output.observations)
    assert not verdict.complete and not verdict.blocked
    assert "NEW_29" in verdict.followup and "OLD_17" in verdict.followup
    corrected = prepare_completion_output(current, "结果是 NEW_29。", CompletionContext())
    assert evaluate(current, corrected.text, observations=corrected.observations).complete
    assert len(current.executed) == 1


@pytest.mark.parametrize("defect", ["missing", "truncated", "conflict", "invalid_type", "invalid_structure"])
def test_unusable_evidence_is_not_an_answer_only_failure(defect: str) -> None:
    session = session_for("FACT_7")
    if defect == "missing":
        session._memory_results.clear()
    elif defect == "truncated":
        session._memory_results["read-1"] = replace(session._memory_results["read-1"], truncated=True)
    elif defect == "invalid_structure":
        commit(session, {"truncated": True})
    elif defect == "conflict":
        commit(session, "FACT_8", "read-2")
    else:
        commit(session, True)
    output = prepare_completion_output(session, "FACT_7", CompletionContext())
    observed = output.observations[requirement().check_id]
    assert not observed.evaluation.passed
    assert observed.evaluation.reason_code != "answer_text_missing"


def test_existing_exact_bindings_and_explanation_overlap_protection_remain() -> None:
    session = session_for("A_17")
    exact = require_tool_success("read_file", answer_pointer="/result", result_pointer="/fact")
    schema = explanation_schema()
    properties = schema["properties"]
    assert isinstance(properties, dict)
    properties["result"] = {"type": "string"}
    session.policy = replace(session.policy,
        required_checks=(declaration("json_explanation", json.dumps(schema)), exact))
    draft = "Original analysis without repeated metadata."
    output = prepare_completion_output(session, draft, CompletionContext())
    assert parse_json_object(output.text) == {"explanation": draft, "result": "A_17"}
    session.policy = replace(session.policy, required_checks=(session.policy.required_checks[0],
        require_tool_success("read_file", answer_pointer="/explanation", result_pointer="/fact")))
    with pytest.raises(OutputBindingError, match="overlaps"):
        prepare_completion_output(session, draft, CompletionContext())


def test_containment_is_explicit_canonical_and_scoped_to_explanation() -> None:
    rule = requirement()
    assert parse_tool_success_check(rule.declaration) == rule
    assert '"contains":true' in rule.declaration
    old = ToolSuccessRequirement("read_file", answer_pointer="/result", result_pointer="/fact")
    assert '"contains"' not in old.declaration and parse_tool_success_check(old.declaration) == old
    with pytest.raises(ValueError, match="/explanation"):
        replace(rule, answer_pointer="/result")
    with pytest.raises(ValueError, match="canonical"):
        parse_tool_success_check(old.declaration.replace('"line":null', '"contains":false,"line":null'))
    session = session_for("A_17")
    session.policy = replace(session.policy, required_checks=(rule.declaration,))
    with pytest.raises(OutputBindingError, match="requires json_explanation"):
        prepare_completion_output(session, "A_17", CompletionContext())


@pytest.mark.parametrize("pattern,three_passes", [(TWO_PARAGRAPHS, False), (AT_LEAST_TWO, True)])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_schema_distinguishes_real_paragraphs_from_literal_escapes(
    pattern: str, three_passes: bool, newline: str,
) -> None:
    check = declaration("json_explanation", json.dumps(explanation_schema(pattern)))
    drafts: list[tuple[str, bool]] = [(f"观察过程。{newline}{newline}实际结果。", True),
              (f"第一段。{newline}{newline}第二段。{newline}{newline}第三段。", three_passes),
              (r"观察过程。\n\n实际结果。", False), (f"观察过程。{newline}{newline} \t", False),
              (f"路径 `C:\\new` 和字面 `\\n`。{newline}{newline}结果保留。", True)]
    for draft, expected in drafts:
        answer: JsonObject = {"explanation": draft}
        passed, reason, feedback = evaluate_output(check, json.dumps(answer, ensure_ascii=False))
        assert passed is expected
        if not passed:
            assert reason == "output_format_invalid" and draft in feedback
            assert "explanation" in feedback and "Reuse committed" in feedback


def test_combined_answer_repairs_preserve_whitespace_paths_and_code() -> None:
    session = session_for(284, pattern=TWO_PARAGRAPHS)
    draft = "过程及路径 `C:\\new`。\n\n结果284，示例 `\\n`。"
    output = prepare_completion_output(session, draft, CompletionContext())
    assert parse_json_object(output.text)["explanation"] == draft
    assert evaluate(session, output.text, observations=output.observations).complete
