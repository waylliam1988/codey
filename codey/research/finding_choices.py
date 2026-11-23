"""Narrow finding selection to immutable reads in the current frozen scope."""
from __future__ import annotations

from dataclasses import replace

from codey.knowledge.model import canonical_json
from codey.research.answer_contract import AnswerScope, AnswerSessionView
from codey.research.finding_stage import comparison_stage
from codey.research.passages import read_passage_refs
from codey.toolchain.tool_spec import ToolSpec, _check_spec_value_against_schema, freeze_spec_parameters
from codey.types.json_value import JsonValue, ToolArgs, canonical_json_object, decode_json_object, parse_json_object


def resolve_finding_passages(args: ToolArgs, scope: AnswerScope, spec: ToolSpec) -> ToolArgs:
    selection = canonical_json_object(dict(spec.parameters).get("passage_ref", {})).get("enum")
    if not isinstance(selection, list):
        return args
    aliases = {label: ref for label, ref in scope.aliases if label in selection}
    if not aliases:
        return args
    lowered = dict(args)
    if isinstance(ref := args.get("passage_ref"), str):
        lowered["passage_ref"] = aliases.get(ref, ref)
    if isinstance(extra := args.get("additional_passage_refs"), list):
        lowered["additional_passage_refs"] = [aliases.get(ref, ref) if isinstance(ref, str) else ref for ref in extra]
    return lowered


def finding_choices(spec: ToolSpec, scope: AnswerScope, session: AnswerSessionView) -> ToolSpec | None:
    rules, pending = comparison_stage(session)
    if rules and not pending:
        return None
    if not scope.passage_refs:
        return spec
    parameters = canonical_json_object(dict(spec.parameters))
    selectors: list[JsonValue] = [label for label, _ in scope.aliases]
    selectors.extend(scope.passage_refs)
    parameters["passage_ref"] = {**decode_json_object(parameters["passage_ref"]), "enum": selectors,
        "description": "Use the short evidence label eN shown with the passage. The host resolves its exact receipt."}
    extras = decode_json_object(parameters["additional_passage_refs"])
    parameters["additional_passage_refs"] = {**extras, "items": {"type": "string", "enum": selectors}}
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
    example = {"tool": spec.name, "args": {"claim": "One complete scoped finding.", "passage_ref": scope.aliases[0][0]}}
    return freeze_spec_parameters(replace(spec, parameters=tuple(parameters.items()), required=required,
        json_examples=(canonical_json(example),), description=spec.description.replace(
            "Copy passage_ref=p... from the read result.", "Supply passage_ref=eN from the frozen evidence menu.")))
