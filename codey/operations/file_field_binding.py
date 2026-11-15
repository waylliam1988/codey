"""One declared receipt selector governs exact file copies and validation."""
from __future__ import annotations

from collections.abc import Mapping

from codey.codemode.json_edit import JsonNumber, json_source_value, replace_json_fields
from codey.operations.task_session import TaskSession
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import ToolObservation, ToolSuccessRequirement, parse_tool_success_check
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, JsonValue


def file_rules(checks: tuple[str, ...], path: str = "") -> tuple[ToolSuccessRequirement, ...]:
    return tuple(rule for check in checks if (rule := parse_tool_success_check(check)) is not None
                 and rule.file_path and (not path or rule.file_path == path))


def selected_value(rule: ToolSuccessRequirement, observation: ToolObservation) -> JsonValue | JsonNumber:
    if not observation.evaluation.passed:
        raise ValueError(observation.evaluation.feedback)
    value = observation.value
    if not isinstance(value, str):
        raise ValueError("file text binding requires a complete selected string")
    if rule.value_type == "string":
        return value
    number = JsonNumber(value)
    if rule.value_type == "integer" and ("." in value or "e" in value.lower()):
        raise ValueError("declared integer requires an integer source lexeme")
    return number


def validate_field(rule: ToolSuccessRequirement, observation: ToolObservation, content: str) -> RequirementEvaluation:
    try:
        expected = selected_value(rule, observation)
        actual = json_source_value(content, rule.answer_pointer or "")
        matches = (isinstance(actual, JsonNumber) and actual.numeric_key() == expected.numeric_key()
                   if isinstance(expected, JsonNumber) else type(actual) is type(expected) and actual == expected)
        if rule.value_type == "integer" and isinstance(actual, JsonNumber):
            matches = matches and "." not in actual.text and "e" not in actual.text.lower()
        if not matches:
            raise ValueError(f"field must match its source as JSON {rule.value_type}")
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        return RequirementEvaluation("file_field_mismatch", f"{rule.file_path}{rule.answer_pointer}: {exc}. "
                                     + rule.guidance, observation.evaluation.evidence_refs)
    return RequirementEvaluation(evidence_refs=observation.evaluation.evidence_refs)


def bound_content(session: TaskSession, path: str, baseline: str, document: JsonObject | None) -> str:
    rules = file_rules(session.policy.required_checks, path)
    if not rules:
        raise ValueError("No receipt-bound file fields are declared for this path")
    changes: dict[str, JsonValue | JsonNumber] = {}
    for rule in rules:
        observation = rule.observe(session.executed, results=session._memory_results, browser_document=document)
        key = (rule.answer_pointer or "")[1:].replace("~1", "/").replace("~0", "~")
        value = selected_value(rule, observation)
        if key in changes and changes[key] != value:
            raise ValueError("Conflicting receipt bindings target the same file field")
        changes[key] = value
    return replace_json_fields(baseline, changes)


def validate_content(checks: tuple[str, ...], path: str, content: str,
                     records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                     document: JsonObject | None) -> str:
    for rule in file_rules(checks, path):
        observation = rule.observe(records, results=results, browser_document=document)
        outcome = validate_field(rule, observation, content)
        if not outcome.passed:
            return outcome.feedback
    return ""
