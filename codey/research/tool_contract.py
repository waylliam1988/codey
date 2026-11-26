"""Research domain tool validation and scoped declarations."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from codey.research.knowledge_schema import (
    object_schema,
    report_sections_schema,
    schema_object,
    schema_strings,
)
from codey.research.tool_declarations import (
    STRUCTURED_REPORT_ARGS as STRUCTURED_REPORT_ARGS,
)
from codey.research.tool_declarations import (
    STRUCTURED_REPORT_EXAMPLE as STRUCTURED_REPORT_EXAMPLE,
)
from codey.research.tool_declarations import (
    TOOL_CONTRACTS as TOOL_CONTRACTS,
)
from codey.research.tool_declarations import (
    ToolContract as ToolContract,
)
from codey.research.tool_declarations import (
    knowledge_read_id_guidance as knowledge_read_id_guidance,
)
from codey.research.tool_names import QUESTION_BOUND_TOOLS
from codey.toolchain.spec_model import EXECUTION_CLAIMS_GUIDANCE, ToolSpec
from codey.types.json_value import (
    JsonObject,
    ToolArgs,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_string,
)

PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_TOO_MANY_TOOLS = "too_many_tools"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_SEARCH_LEAK = "native_search_leak"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"

@dataclass(frozen=True)
class ContractResult:
    ok: bool
    args: ToolArgs = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""


def question_bound_spec(spec: ToolSpec, *, question_refs: tuple[str, ...]) -> ToolSpec | None:
    """Bind this turn's research actions to its registered questions."""
    if spec.name not in QUESTION_BOUND_TOOLS and spec.evidence_route != "source_read":
        return spec
    if not question_refs:
        return None
    from codey.toolchain.spec_model import freeze_spec_parameters

    parameters = {**dict(spec.parameters), "question_ref": {"type": "string", "enum": list(question_refs)}}
    keys = (*spec.required, "question_ref")
    examples = []
    for text in spec.json_examples:
        example = parse_json_object(text)
        schema_object(example, "args")["question_ref"] = question_refs[0]
        examples.append(json.dumps(example, separators=(",", ":")))
    return freeze_spec_parameters(replace(spec, parameters=tuple(parameters.items()), required=keys,
        json_examples=tuple(examples), description=spec.description
        + " Select question_ref from the registered questions: " + ", ".join(question_refs) + "."))


def done_spec_for_purpose(spec: ToolSpec, purpose: str, *, task: str = "", questions: tuple[str, ...] = ()) -> ToolSpec:
    """Select one complete done contract before freezing, never patch pieces later."""
    example: ToolArgs
    if purpose in {"ordinary", "chat"}:
        removed = {"source_gaps", "execution_claims"} if purpose == "chat" else {"source_gaps"}
        parameters = tuple((key, value) for key, value in spec.parameters if key not in removed)
        fields = dict(parameters)
        description = "Finish the task with the complete answer in summary, in the user's requested format. "
        if "knowledge_refs" in fields:
            description += "Optional knowledge_refs selects exact saved versions read for this task. "
        if "execution_claims" in fields:
            description += EXECUTION_CLAIMS_GUIDANCE
        description += "Use actual source coverage to explain any limitations."
        return replace(spec, parameters=parameters, description=description)
    if purpose == "evidence":
        schema = object_schema({"summary": {"type": "string"}}, "summary")
        example = {"tool": "done", "args": {"summary": "Saved findings and remaining evidence gaps"}}
        description = ("End evidence investigation with a brief receipt of saved findings and specific remaining gaps. "
                       "The parent owns final synthesis; do not write its report in this phase.")
    elif purpose == "report":
        coverage = schema_object(schema_object(report_sections_schema(), "properties"), "coverage")
        item_schema = schema_object(coverage, "items")
        properties = schema_object(item_schema, "properties")
        properties.pop("answer")
        required_fields = [key for key in schema_strings(item_schema, "required")
                           if key not in {"answer", "investigations"}]
        if questions:
            schema_object(properties, "question_ref")["enum"] = [
                f"q{index}" for index in range(1, len(questions) + 1)]
            coverage.update(minItems=len(questions), maxItems=len(questions))
        refs: list[str] = []
        if task:
            from codey.research.task_requirements import coverage_requirements

            requirements: Sequence[Mapping[str, object]] = coverage_requirements(task)
            refs = [require_json_string(row["task_ref"], "task_ref") for row in requirements]
            schema_object(properties, "task_refs").update(
                items=canonical_json_object({"type": "string", **({"enum": refs} if refs else {})}), maxItems=len(refs))
            required_fields.append("task_refs")
        item_schema["required"] = list(required_fields)
        schema = object_schema({"report_sections": object_schema({"coverage": coverage}, "coverage")}, "report_sections")
        selected: list[JsonObject] = [{"question_ref": "q1", "state": "partial", "findings": ["f1"]}]
        if task:
            selected[0]["task_refs"] = list(refs)
        if questions:
            selected.extend({"question_ref": f"q{index}", "state": "not_investigated", "findings": [],
                             **({"task_refs": []} if task else {})}
                            for index in range(2, len(questions) + 1))
        example = canonical_json_object({"tool": "done", "args": {"report_sections": {"coverage": selected}}})
        description = ("Select existing findings once per registered question in report_sections.coverage: "
                       "{question_ref,state,findings,task_refs}. Use raw fN ids bound to that question. "
                       "The application adopts their exact pinned claim text, with no final answer rewriting. "
                       "Use evidence_found for an answered question, partial for a supported but incomplete answer, "
                       "or not_investigated/unanswered_after_search with findings=[] for gaps. "
                       "Investigations are optional; actual question-bound receipts are supplied by the application. "
                       "Explicit investigations must still be real and belong to this question. "
                       "Actual attempts turn not_investigated into unanswered_after_search; attempts are not evidence. "
                       "Gap notices, limitations, citations and source metadata are compiled, not model-authored. "
                       "If a claim is wrong, save its supported correction from the original passages and select the new fN. "
                       "Include task_refs selecting the original rN task units answered or explicitly left unresolved. "
                       "Account for every coverage_task_refs rN across the answers; this records claimed coverage, not verification. "
                       "The application checks recognized standalone report format requirements on the conclusion; "
                       "do not register them as questions or attach their rN ids. "
                       "To repair task_refs, edit existing rows; never append a duplicate question row. "
                       "Do not supply answer, summary, evidence, counter or source_quality prose.")
    else:
        raise ValueError("unknown done purpose")
    return replace(spec, parameters=tuple(schema_object(schema, "properties").items()), required=tuple(schema_strings(schema, "required")),
                   json_examples=(json.dumps(example, separators=(",", ":")),), description=description)


def validate_tool_args(tool: str, args: ToolArgs) -> ContractResult:
    """Direct callers use the same ToolSpec checker as the shared kernel."""
    from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec

    spec = spec_for_tool(tool)
    if spec is None:
        return ContractResult(False, error=f"unknown tool: {tool}", error_kind=PROTOCOL_UNKNOWN_TOOL)
    try:
        args = decode_json_object(args)
    except ValueError as exc:
        return ContractResult(False, error=str(exc), error_kind=PROTOCOL_INVALID_ARGS)
    error = validate_args_with_spec(spec, args)
    return ContractResult(not error, args=dict(args) if not error else {}, error=error,
                          error_kind=PROTOCOL_INVALID_ARGS if error else "")
