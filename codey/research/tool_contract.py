"""Research domain tool contracts, projected into the shared ToolSpec registry."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from codey.research.knowledge_schema import (
    array_schema,
    comparison_schema,
    condition_array_schema,
    condition_map_schema,
    knowledge_reference_schema,
    mutation_schema,
    note_evidence_schema,
    object_schema,
    report_sections_schema,
    schema_object,
    schema_strings,
)
from codey.research.questions import QUESTION_BOUND_TOOLS
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import JsonObject, ToolArgs, canonical_json_object, decode_json_object

PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_TOO_MANY_TOOLS = "too_many_tools"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_SEARCH_LEAK = "native_search_leak"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"

STRUCTURED_REPORT_ARGS: ToolArgs = {"summary": "Supported conclusion [f1]", "report_sections": {
    "evidence": ["Saved finding with its conditions [f1]"],
    "counter": ["Observed limitation and its scope [f1]"],
    "coverage": [{"question_ref": "q1", "state": "partial", "answer": "Answer and remaining gap",
                  "findings": ["f1"], "investigations": []}]}}
STRUCTURED_REPORT_EXAMPLE = json.dumps({"tool": "done", "args": STRUCTURED_REPORT_ARGS}, separators=(",", ":"))


def knowledge_read_id_guidance(*, source_tool: str = "") -> str:
    source = f" by {source_tool}" if source_tool else ""
    return (
        f"For knowledge_read, pass the note id exactly as returned{source}; "
        "do not prepend facts/ or append .md. "
        "An opened-source sN or read-passage pN is a reading locator, not a stored knowledge id."
    )


@dataclass(frozen=True)
class ToolContract:
    name: str
    input_schema: Mapping[str, object]
    example: str = ""
    description: str = ""
    parallel_safe: bool = False

@dataclass(frozen=True)
class ContractResult:
    ok: bool
    args: ToolArgs = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""


TOOL_CONTRACTS = {
    "research_plan": ToolContract(
        name="research_plan", input_schema=object_schema({
            'questions': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 8},
        }, 'questions'),
        example='{"tool":"research_plan","args":{"questions":["What is the answer and its evidence scope?"]}}',
        description="Register 1..8 questions covering the user request before investigation. Returns stable q1, q2, etc. "
                    "Register distinct factual questions, not workflow steps such as finding sources, quoting evidence "
                    "or writing a report. A single factual question normally needs just one registered question, "
                    "including its evidence and applicability conditions. Do not restate it as another question about "
                    "what the documentation says. Split only independently answerable topics requested by the user. "
                    "Keep questions concise. Later calls may append questions, never replace or reorder them. "
                    "The final report accounts for every question with evidence or a specific gap; planning is not completion.",
    ),
    "submit_research_answer": ToolContract(
        name="submit_research_answer", input_schema=object_schema({
            "markdown": {"type": "string", "minLength": 12, "maxLength": 64000},
            "state": {"type": "string", "enum": ["answered", "partial", "unresolved"]},
        }, "markdown", "state"),
        example='{"tool":"submit_research_answer","args":{"state":"unresolved","markdown":"The investigation could not establish the answer."}}',
        description="Submit the complete Research answer under its frozen evidence contract.",
    ),
    "web_search": ToolContract(
        name="web_search",
        input_schema=object_schema({
            'query': {'type': 'string'},
        }, 'query'),
        example='{"tool":"web_search","args":{"query":"..."}}',
        description="search the web for ranked results",
    ),
    "open_url": ToolContract(
        name="open_url",
        input_schema=object_schema({
            'url': {'type': 'string'},
            'offset': {'type': 'integer'},
            'limit': {'type': 'integer'},
            'pages': {'type': 'string'},
        }, 'url'),
        example='{"tool":"open_url","args":{"url":"https://...","offset":0,"limit":6000,"pages":"1-5"}}',
        description="read a page's text; for PDFs, pages selects bounded page ranges",
    ),
    "source_search": ToolContract(
        name="source_search",
        input_schema=object_schema({
            'url': {'type': 'string'},
            'query': {'type': 'string'},
            'limit': {'type': 'integer'},
        }, 'query', 'url'),
        example='{"tool":"source_search","args":{"url":"https://...","query":"...","limit":6}}',
        description="search within a source already opened with open_url; returns locators, offsets, and PDF pages",
    ),
    "browser_snapshot": ToolContract(
        name="browser_snapshot", input_schema=object_schema({
            "max_elements": {"type": "integer", "minimum": 1, "maximum": 80},
            "max_text": {"type": "integer", "minimum": 500, "maximum": 12000},
        }),
        example='{"tool":"browser_snapshot","args":{}}',
        description="observe the retained source page as bounded text with semantic element refs; take a fresh snapshot after navigation or actions",
    ),
    "browser_read": ToolContract(
        name="browser_read", input_schema=object_schema({
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 500, "maximum": 12000},
        }),
        example='{"tool":"browser_read","args":{}}',
        description="capture and read the currently bound browser page without navigating; use after revealing content to obtain citable source passages",
    ),
    "browser_click": ToolContract(
        name="browser_click", input_schema=object_schema({"ref": {"type": "string"}}, "ref"),
        example='{"tool":"browser_click","args":{"ref":"<ref from latest browser_snapshot>"}}',
        description="click an element ref from the latest browser_snapshot once, then observe the resulting page state",
    ),
    "browser_fill": ToolContract(
        name="browser_fill", input_schema=object_schema({"ref": {"type": "string"}, "value": {"type": "string"}}, "ref", "value"),
        example='{"tool":"browser_fill","args":{"ref":"<ref from latest browser_snapshot>","value":"福州小吃"}}',
        description="fill a textbox ref from the latest browser_snapshot and verify its value; stale refs are rejected",
    ),
    "browser_scroll": ToolContract(
        name="browser_scroll", input_schema=object_schema({
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "amount": {"type": "integer", "minimum": 1, "maximum": 20000},
        }, "direction", "amount"),
        example='{"tool":"browser_scroll","args":{"direction":"down","amount":700}}',
        description="scroll the retained page by a bounded amount; take a fresh browser_snapshot afterward",
    ),
    "browser_wait": ToolContract(
        name="browser_wait", input_schema=object_schema({
            "condition": {"type": "string", "enum": ["text", "selector", "url_contains"]},
            "value": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 30000},
        }, "condition", "value", "timeout_ms"),
        example='{"tool":"browser_wait","args":{"condition":"text","value":"全文","timeout_ms":5000}}',
        description="wait for one explicit page condition with a bounded timeout; timeout is a truthful failure",
    ),
    "knowledge_search": ToolContract(
        name="knowledge_search",
        input_schema=object_schema({
            'query': {'type': 'string'},
            'depth': {'type': 'integer'},
        }, 'query'),
        example='{"tool":"knowledge_search","args":{"query":"..."}}',
        description="search scoped knowledge FIRST; returns pinned versions, evidence, conditions and recall paths. "
                    "Try mechanism or constraint queries to investigate cross-topic links; a path is not proof.",
        parallel_safe=True,
    ),
    "knowledge_read": ToolContract(
        name="knowledge_read",
        input_schema=object_schema({
            'id': {'type': 'string'},
            'revision': {'type': 'integer'},
            'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 500, 'maximum': 12000},
        }, 'id'),
        example='{"tool":"knowledge_read","args":{"id":"<note id>"}}',
        description=("read a structured object and its pinned revision, conditions, provenance and references; "
                     "also accepts legacy note ids. Source reads accept offset/limit for bounded pinned windows; "
                     "source and evidence reads yield original passages for citations. " + knowledge_read_id_guidance()),
        parallel_safe=False,
    ),
    "knowledge_write": ToolContract(
        name="knowledge_write",
        input_schema=object_schema({
            'type': {'type': 'string', 'enum': ['source', 'fact', 'hypothesis', 'conclusion', 'question', 'synthesis', 'decision', 'implementation', 'verification', 'project_note', 'commit', 'note']},
            'title': {'type': 'string'},
            'body': {'type': 'string', 'description': 'Required note text, even when evidence is supplied.'},
            'id': {'type': 'string', 'description': 'Optional existing note_id to update, exactly as returned by knowledge_write. Omit for a new note; never invent it or use a structured object id.'},
            'sources': array_schema({'type': 'string'}),
            'tags': array_schema({'type': 'string'}),
            'aliases': array_schema({'type': 'string'}),
            'relations': array_schema({'type': 'object'}),
            'evidence': array_schema(note_evidence_schema()),
            'open_questions': array_schema({'type': 'string'}),
            'confidence': {'type': 'number'},
            'retrieved_at': {'type': 'string'},
            'valid_until': {'type': 'string'},
            'status': {'type': 'string'},
            'conditions': condition_map_schema(),
        }, 'body', 'title', 'type'),
        example='{"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"one supported finding","sources":["https://..."],"evidence":[{"claim":"one scoped finding in the requested answer language","source_url":"https://...","excerpt":"exact contiguous result or limitation copied from opened text","stance":"supports"}],"conditions":{"applicability":{"state":"not_extracted"}}}}',
        description="save small source/fact/hypothesis/conclusion/question notes; final reports must use done. "
                    "Omit id for a new note. To update, id must be an existing note_id from knowledge_write, "
                    "not a structured claim/source/evidence id. "
                    "For sourced findings use type=fact, sources=[opened URL strings], and "
                    "evidence=[{claim:your conclusion,source_url:opened URL,excerpt:exact original text,stance:supports}]. "
                    "For a new note, omitted sources are copied from explicit evidence[].source_url values. "
                    "Do not relabel an unsupported fact as hypothesis to bypass missing sources. "
                    "conditions must be an object: {name:{state:reported,value:...}} or state "
                    "not_reported/not_extracted/not_applicable. Keep unknowns unknown. "
                    "confidence is not evidence strength. Use knowledge_commit with restricted scope for user observations.",
    ),
    "save_finding": ToolContract(
        name="save_finding", input_schema=object_schema({
            'claim': {'type': 'string', 'minLength': 12, 'maxLength': 4000},
            'source_ref': {'type': 'string'},
            'passage_ref': {'type': 'string'},
            'additional_passage_refs': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3,
                                        'description': 'Additional read passages from the same exact source version, if needed to support the whole claim.'},
            'conditions': condition_array_schema(),
            'comparison': comparison_schema(),
        }, 'claim', 'passage_ref'),
        example='{"tool":"save_finding","args":{"claim":"One complete scoped finding","passage_ref":"p..."}}',
        description="Save ONE finding supported by exact opened passages. Copy passage_ref=p... "
                    "from the read result. The application saves the original excerpt and pinned source version. "
                    "Already read passages can support another question without reopening the source. "
                    "If the full explanation spans passages, add up to 3 additional_passage_refs from that same version. "
                    "Each excerpt is saved separately; select enough evidence for every part of the claim. "
                    "Write the locally supported answer once, in the requested language; selection adopts this exact text. "
                    "Separate independently supported behaviors, such as a version boundary and a distinct defect. "
                    "Keep one conditional relationship complete even across sentences; do not split by punctuation. "
                    "Keep conditions and uncertainty in claim; do not transfer a result between comparators. "
                    "Copy version literals from selected passages; matching a version does not establish its behavior. "
                    "Optional conditions=[{name:...,state:reported,value:...}] (or not_reported/not_extracted/not_applicable). "
                    "For comparisons optionally supply subject, factor, comparator, endpoint, direction, uncertainty; "
                    "use unknown for absent information. A successful save remains unreviewed. "
                    "Cite the returned [f...] in the report; do not copy object ids or build a source table.",
    ),
    "save_inference": ToolContract(
        name="save_inference", input_schema=object_schema({
            'statement': {'type': 'string', 'minLength': 12, 'maxLength': 4000},
            'premise_refs': {'type': 'array', 'items': {'type': 'string', 'pattern': '^f[1-9][0-9]*$'}, 'minItems': 1, 'maxItems': 16},
            'limitations': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        }, 'limitations', 'premise_refs', 'statement'),
        example='{"tool":"save_inference","args":{"statement":"A conditional hypothesis linking these findings",'
                '"premise_refs":["f1","f2"],"limitations":"No direct test of the proposed link"}}',
        description="Save a NEW candidate inference from previously saved or read fN premises. "
                    "Explain its conditions and limitations; do not present a plausible link as established evidence. "
                    "Returns a short fN pinned to exact premise versions. Remains candidate and unreviewed.",
    ),
    "knowledge_link": ToolContract(
        name="knowledge_link",
        input_schema=object_schema({
            'src': {'type': 'string'},
            'dst': {'type': 'string'},
            'kind': {'type': 'string'},
        }, 'dst', 'src'),
        example='{"tool":"knowledge_link","args":{"src":"<note id>","dst":"<note id or exact title>","kind":"supports"}}',
        description="",
    ),
    "knowledge_commit": ToolContract(
        name="knowledge_commit", input_schema=object_schema({
            'commit_id': {'type': 'string'},
            'changes': array_schema(mutation_schema()),
        }, 'changes', 'commit_id'),
        example='{"tool":"knowledge_commit","args":{"commit_id":"question-1","changes":[{"expected_revision":0,"value":{"id":"open-question-1","kind":"question","content":"What would refute this explanation?","origin":"model_inference"}}]}}',
        description="atomically create or revise long-term objects. Reuse commit_id only for identical content. "
                    "Each change has expected_revision (0 for new) and value: id, kind "
                    "(entity/source/evidence/observation/claim/inference/question/answer), content, origin "
                    "(source/observed_result/author_interpretation/model_extraction/model_inference/user_statement/experiment); "
                    "optional title, status (candidate/active/stale/withdrawn/superseded/contradicted), aliases, "
                    "scope (global/project/session/personal), scope_id, conditions, provenance, references. "
                    "References use {target,revision,relation}: about, from_source, supported_by, refuted_by, "
                    "limited_by, depends_on, uses, motivated_by, supersedes, same_study. "
                    "Conditions use {name:{state:reported/not_reported/not_extracted/not_applicable,value:...}}. "
                    "Read before revising; never merge by similarity. User statements require restricted scope. "
                    "Active claims require evidence; hypotheses stay candidates. Old answers cannot support claims.",
    ),
    "done": ToolContract(
        name="done",
        input_schema=object_schema({
            'summary': {'type': 'string'},
            'open_questions': array_schema({'type': 'string'}),
            'knowledge_refs': array_schema(knowledge_reference_schema()),
            'report_sections': report_sections_schema(),
        }, 'summary'),
        example='{"tool":"done","args":{"summary":"<the full human-readable report>","open_questions":["<bounded follow-up research question>"]}}',
        description="For a structured research report, summary is the conclusion with citations, and "
                    "report_sections supplies evidence, counter as text arrays, and coverage as question records "
                    "{question_ref:q1,state:partial,answer:answer or gap,findings:[f1],investigations:[a1]}. States: evidence_found, partial, "
                    "not_investigated, unanswered_after_search. Evidence states need findings; gap states need an empty findings array. "
                    "Cite selected findings with [f...] from save_finding; the application generates source numbers, "
                    "source table, exact knowledge versions, headings and paragraph breaks. All evidence checks still apply. "
                    "Without report_sections, summary is the complete final answer.",
    ),
}


def question_bound_spec(spec: ToolSpec, *, question_refs: tuple[str, ...]) -> ToolSpec:
    """Bind this turn's research actions to its registered questions."""
    if spec.name not in QUESTION_BOUND_TOOLS:
        return spec
    from codey.toolchain.tool_spec import freeze_spec_parameters

    parameters = {**dict(spec.parameters), "question_ref": {"type": "string", "enum": list(question_refs)}}
    keys = (*spec.required, "question_ref")
    examples = []
    for text in spec.json_examples:
        example = decode_json_object(json.loads(text))
        schema_object(example, "args")["question_ref"] = question_refs[0]
        examples.append(json.dumps(example, separators=(",", ":")))
    return freeze_spec_parameters(replace(spec, parameters=tuple(parameters.items()), required=keys,
        json_examples=tuple(examples), description=spec.description
        + " Select question_ref from the registered questions: " + ", ".join(question_refs) + "."))


def done_spec_for_purpose(spec: ToolSpec, purpose: str, *, task: str = "", questions: tuple[str, ...] = ()) -> ToolSpec:
    """Select one complete done contract before freezing, never patch pieces later."""
    example: ToolArgs
    if purpose == "ordinary":
        return spec
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

            refs = [row["task_ref"] for row in coverage_requirements(task)]
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


