"""Assemble declared JSON fields from the existing committed tool receipts."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from codey.codemode.json_edit import JsonNumber, replace_json_fields
from codey.operations.browser.browser_text_report import (
    BrowserMetadata,
    MetadataPending,
    observe_metadata,
    report_scope,
)
from codey.operations.browser.browser_upload import (
    UploadEvidence,
    bound_upload_explanation,
    upload_evidence,
    upload_scope,
)
from codey.operations.completion_context import CompletionContext
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import has_explanation, is_output_check, schema_for
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import (
    ToolObservation,
    ToolSuccessRequirement,
    argument_digest,
    decode_answer,
    parse_tool_success_check,
)
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.toolchain.result_schema import validate_output
from codey.types.json_value import JsonValue, decode_json_value


class OutputBindingError(ValueError):
    """The declared destinations cannot represent all selected values."""


@dataclass(frozen=True)
class CompletionOutput:
    text: str
    observations: Mapping[str, ToolObservation]
    metadata: BrowserMetadata | MetadataPending | None = None


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
    file_values: dict[str, JsonValue | JsonNumber] = {}
    explanation = has_explanation(session.policy.required_checks)
    if explanation and session.policy.strict_research:
        raise OutputBindingError("json_explanation cannot replace the strict Research answer contract.")
    schema = next((schema_for(check) for check in session.policy.required_checks if is_output_check(check)), None)
    scope = upload_scope(session)
    bound_upload = (upload_evidence(session, scope, context.browser_document)
                    if scope is not None and bound_upload_explanation(session.policy.required_checks) else None)
    for declaration in session.policy.required_checks:
        try:
            requirement = parse_tool_success_check(declaration)
        except (ValueError, TypeError) as exc:
            raise OutputBindingError("Invalid declared tool requirement.") from exc
        if requirement is None:
            continue
        if (requirement.answer_contains or requirement.omit_from_explanation) and not explanation:
            raise OutputBindingError("An explanation occurrence rule requires json_explanation.")
        if requirement.browser_text is not None and not requirement.file_path and not requirement.report_label and not explanation:
            raise OutputBindingError("Browser text fields require json_explanation.")
        if explanation and not requirement.file_path and not requirement.answer_contains and requirement.answer_pointer is not None and (
                requirement.answer_pointer == "" or requirement.answer_pointer.split("/")[1] == "explanation"):
            raise OutputBindingError("A receipt binding overlaps the declared /explanation text destination.")
        binding_records = _upload_binding_records(requirement, records, bound_upload)
        observation = requirement.observe(binding_records, results=results, browser_document=context.browser_document)
        _validate_report_destination(requirement, session.policy.strict_research)
        if requirement.file_path and observation.evaluation.passed and requirement.answer_pointer:
            from codey.operations.file_field_binding import selected_value

            pointer = requirement.answer_pointer
            path = tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/"))
            selected_schema = _field_schema(schema, path)
            if selected_schema is not None:
                value = selected_value(requirement, observation)
                fields.append(_Field(path, None))
                key = path[0]
                if key in file_values and file_values[key] != value:
                    raise OutputBindingError("Conflicting declared file facts target the same final answer field.")
                file_values[key] = value
        if requirement.answer_pointer is not None and not requirement.file_path and not requirement.report_label and not requirement.answer_contains and observation.evaluation.passed:
            pointer = requirement.answer_pointer
            path = tuple(part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")) if pointer else ()
            selected_schema = _field_schema(schema, path) if requirement.binds_browser_fields else None
            try:
                if selected_schema is not None:
                    validate_output(pointer, selected_schema, observation.value)
                fields.append(_Field(path, observation.value))
            except ValueError as exc:
                observation = ToolObservation(RequirementEvaluation("observation_selector_unavailable",
                    f"The current-page receipt does not supply a valid answer field {pointer!r}. "
                    f"Read result selector {requirement.result_pointer!r} with the required JSON type and constraints. {exc} "
                    + requirement.execution_guidance, observation.evaluation.evidence_refs))
        observations[requirement.check_id] = observation
    if (not fields and not explanation) or session.policy.strict_research:
        report = report_scope(session)
        metadata = observe_metadata(report, records, results, context.browser_document) if report is not None else None
        if report is not None and isinstance(metadata, BrowserMetadata):
            return CompletionOutput(metadata.render(text, chinese=report.chinese), observations, metadata)
        if (schema is None and scope is not None and scope.report_hash
                and isinstance(evidence := upload_evidence(session, scope, context.browser_document), UploadEvidence)):
            text = evidence.render(chinese=scope.chinese)
        return CompletionOutput(text, observations, metadata)
    proposed: JsonValue
    if explanation:
        proposed = {"explanation": explanation_text(text, session.policy.required_checks, observations)}
    else:
        try:
            proposed = decode_answer(text)
        except (ValueError, TypeError):
            proposed = {}
    assembled = _assemble(proposed, schema, fields)
    rendered = json.dumps(assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if file_values:
        rendered = replace_json_fields(rendered, file_values)
    return CompletionOutput(rendered, observations)


def _validate_report_destination(rule: ToolSuccessRequirement, strict_research: bool) -> None:
    if rule.report_label and not strict_research:
        raise OutputBindingError("Markdown receipt fields require the strict Research answer contract.")


def explanation_text(text: str, checks: tuple[str, ...], observations: Mapping[str, ToolObservation]) -> str:
    from codey.policies.tool_requirement import omit_selected_text

    omitted: list[str] = []
    required: list[str] = []
    for check in checks:
        rule = parse_tool_success_check(check)
        observation = observations.get(rule.check_id) if rule is not None else None
        if rule is None or observation is None or not observation.evaluation.passed or not isinstance(observation.value, str):
            continue
        if rule.omit_from_explanation and observation.value:
            omitted.append(observation.value)
        if rule.answer_contains:
            required.append(observation.value)
    for value in sorted(set(omitted), key=len, reverse=True):
        if not any(value in fact for fact in required):
            text = omit_selected_text(text, value)
    return text


def _upload_binding_records(requirement: ToolSuccessRequirement, records: dict[str, ExecutionRecord],
                            evidence: UploadEvidence | str | None) -> dict[str, ExecutionRecord]:
    if not isinstance(evidence, UploadEvidence) or requirement.answer_pointer is None:
        return records
    if requirement.tool == "browser_upload":
        ref = evidence.selection_ref
    elif requirement.tool == "browser_read" and requirement.result_pointer in {"", "/text"}:
        ref = evidence.page_ref
    else:
        return records
    return {identity: row for identity, row in records.items() if row["name"] != requirement.tool or identity == ref}


def _field_schema(schema: JsonValue, path: tuple[str, ...]) -> Mapping[str, object] | None:
    for key in path:
        if not isinstance(schema, dict):
            return None
        if schema.get("type") == "array":
            schema = schema.get("items")
        else:
            properties = schema.get("properties")
            schema = properties.get(key, schema.get("additionalProperties")) if isinstance(properties, dict) else None
    return schema if isinstance(schema, dict) else None


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
        for index in sorted(int(key) for key in children):
            fields = children[str(index)]
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
