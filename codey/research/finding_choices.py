"""Narrow finding selection to immutable reads in the current frozen scope."""
from __future__ import annotations

from dataclasses import replace

from codey.research.answer_contract import AnswerScope, AnswerSessionView
from codey.research.finding_stage import comparison_stage
from codey.research.passages import read_passage_refs
from codey.toolchain.tool_spec import ToolSpec, _check_spec_value_against_schema, freeze_spec_parameters
from codey.types.json_value import JsonValue, canonical_json_object, decode_json_object, parse_json_object


def finding_choices(spec: ToolSpec, scope: AnswerScope, session: AnswerSessionView) -> ToolSpec | None:
    rules, pending = comparison_stage(session)
    if rules and not pending:
        return None
    if not scope.passage_refs:
        return spec
    parameters = canonical_json_object(dict(spec.parameters))
    parameters["passage_ref"] = {**decode_json_object(parameters["passage_ref"]), "enum": list(scope.passage_refs)}
    selected_refs = set(scope.passage_refs)
    sources: dict[str, str] = {}
    for result in session._memory_results.values():
        if not result.ok:
            continue
        refs = set(read_passage_refs(result.canonical.get("read_passages", []))) & selected_refs
        if not refs:
            continue
        source_id = result.canonical.get("source_id")
        if isinstance(source_id, str):
            sources.update((ref, source_id) for ref in refs)
    if selected_refs <= sources.keys():
        parameters["source_ref"] = {**decode_json_object(parameters["source_ref"]),
            "enum": list[JsonValue](sorted(set(sources.values()))),
            "description": "Optional. The selected passage already identifies its original source."}
    choices: list[JsonValue] = []
    comparison = parameters["comparison"]
    for rule in pending:
        arguments = parse_json_object(rule.arguments_json)
        value = arguments.get("comparison")
        if (set(arguments) != {"comparison"} or not isinstance(value, dict)
                or _check_spec_value_against_schema(spec.name, "comparison", comparison, value, strict=True)):
            continue
        if value not in choices:
            choices.append(value)
    required = spec.required
    if choices:
        parameters["comparison"] = {**decode_json_object(comparison), "enum": choices}
        required = tuple(dict.fromkeys((*required, "comparison")))
    return freeze_spec_parameters(replace(spec, parameters=tuple(parameters.items()), required=required))
