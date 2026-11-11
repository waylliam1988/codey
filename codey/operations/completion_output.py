"""Assemble declared JSON fields from the existing committed tool receipts."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from codey.operations.completion_context import CompletionContext
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import has_explanation, is_output_check, schema_for
from codey.policies.tool_requirement import ToolObservation, argument_digest, decode_answer, parse_tool_success_check
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonValue, decode_json_value


class OutputBindingError(ValueError):
    """The declared destinations cannot represent all selected values."""


@dataclass(frozen=True)
class CompletionOutput:
    text: str
    observations: Mapping[str, ToolObservation]


@dataclass(frozen=True)
class _Field:
    path: tuple[str, ...]
    value: JsonValue


def tool_facts(session: TaskSession, context: CompletionContext) -> tuple[dict[str, ExecutionRecord], dict[str, ToolResult]]:
    parent = context.research_prerequisites if session.task_kind == "research" else None
    return ({**(parent.executed if parent is not None else {}), **session.executed},
            {**(parent.results if parent is not None else {}), **session._memory_results})


def prepare_completion_output(session: TaskSession, text: str, context: CompletionContext) -> CompletionOutput:
    records, results = tool_facts(session, context)
    observations: dict[str, ToolObservation] = {}
    fields: list[_Field] = []
    explanation = has_explanation(session.policy.required_checks)
    if explanation and session.policy.strict_research:
        raise OutputBindingError("json_explanation cannot replace the strict Research answer contract.")
    for declaration in session.policy.required_checks:
        try:
            requirement = parse_tool_success_check(declaration)
        except (ValueError, TypeError) as exc:
            raise OutputBindingError("Invalid declared tool requirement.") from exc
        if requirement is None:
            continue
        if explanation and requirement.answer_pointer is not None and (
                requirement.answer_pointer == "" or requirement.answer_pointer.split("/")[1] == "explanation"):
            raise OutputBindingError("A receipt binding overlaps the declared /explanation text destination.")
        observation = requirement.observe(records, results=results)
        observations[requirement.check_id] = observation
        if requirement.answer_pointer is not None and observation.evaluation.passed:
            pointer = requirement.answer_pointer
            path = tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")) if pointer else ()
            fields.append(_Field(path, observation.value))
    if (not fields and not explanation) or session.policy.strict_research:
        return CompletionOutput(text, observations)
    proposed: JsonValue
    if explanation:
        proposed = {"explanation": text}
    else:
        try:
            proposed = decode_answer(text)
        except (ValueError, TypeError):
            proposed = {}
    schema = next((schema_for(check) for check in session.policy.required_checks if is_output_check(check)), None)
    assembled = _assemble(proposed, schema, fields)
    return CompletionOutput(json.dumps(assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False), observations)


def _assemble(proposed: JsonValue, schema: JsonValue, fields: list[_Field]) -> JsonValue:
    direct = [field.value for field in fields if not field.path]
    if direct:
        if any(argument_digest(value) != argument_digest(direct[0]) for value in direct[1:]):
            raise OutputBindingError("Conflicting receipt bindings target the same JSON field.")
        proposed = decode_json_value(direct[0])
    children: dict[str, list[_Field]] = {}
    for field in fields:
        if field.path:
            children.setdefault(field.path[0], []).append(_Field(field.path[1:], field.value))
    if not children:
        return proposed
    declared_type = schema.get("type") if isinstance(schema, dict) else None
    if not direct:
        if declared_type == "array" and not isinstance(proposed, list):
            proposed = []
        elif declared_type == "object" and not isinstance(proposed, dict) or not isinstance(proposed, (list, dict)):
            proposed = {}
    assembled = _assemble_children(proposed, schema, children)
    if direct and argument_digest(assembled) != argument_digest(direct[0]):
        raise OutputBindingError("Overlapping receipt bindings disagree at a JSON destination.")
    return assembled


def _assemble_children(proposed: JsonValue, schema: JsonValue, children: dict[str, list[_Field]]) -> JsonValue:
    if isinstance(proposed, dict):
        properties = schema.get("properties") if isinstance(schema, dict) else None
        for key, fields in sorted(children.items()):
            child_schema = properties.get(key) if isinstance(properties, dict) else None
            proposed[key] = _assemble(proposed.get(key), child_schema, fields)
        return proposed
    if isinstance(proposed, list):
        if any(not key.isascii() or not key.isdecimal() or str(int(key)) != key for key in children):
            raise OutputBindingError("An array destination requires canonical integer indices.")
        items = schema.get("items") if isinstance(schema, dict) else None
        for key, fields in sorted(children.items(), key=lambda item: int(item[0])):
            index = int(key)
            if index > len(proposed):
                raise ValueError("Supply the unbound preceding array elements in done.summary; "
                                 "the application fills receipt-bound elements.")
            value = _assemble(proposed[index] if index < len(proposed) else None, items, fields)
            if index == len(proposed):
                proposed.append(value)
            else:
                proposed[index] = value
        return proposed
    raise OutputBindingError("An overlapping receipt binding targets a child of a scalar value.")
