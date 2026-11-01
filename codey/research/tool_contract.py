"""Research domain tool contracts, projected into the shared ToolSpec registry."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

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
)

PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_TOO_MANY_TOOLS = "too_many_tools"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_SEARCH_LEAK = "native_search_leak"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"

STRUCTURED_REPORT_ARGS: dict[str, Any] = {"summary": "Supported conclusion [f1]", "report_sections": {
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
    input_schema: Mapping[str, Any]
    example: str = ""
    description: str = ""
    parallel_safe: bool = False

@dataclass(frozen=True)
class ContractResult:
    ok: bool
    args: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""


TOOL_CONTRACTS = {
    "research_plan": ToolContract(
        name="research_plan", input_schema=object_schema({
            'questions': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 8},
            'question_details': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                'items': object_schema({'task_refs': array_schema({'type': 'string'}),
                    'work_units': {'type': 'array', 'minItems': 1, 'maxItems': 4,
                                   'items': {'type': 'string', 'minLength': 1, 'maxLength': 800},
                                   'description': 'Optional ordered relationship targets under the SAME parent question. Keep each full OR list and its exceptions together.'},
                    'submission_mode': {'type': 'string', 'enum': ['select', 'inline'],
                        'description': 'Optional explicit contract: select (default) adopts saved/read findings; inline also allows one new supported finding and its declared state in the same submission. Not semantic approval.'}}, 'task_refs'),
                'description': 'One record per question in order. Assign original coverage rN requirements once to enable local submissions.'},
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
            'state': {'type': 'string', 'enum': ['answered', 'partial', 'unresolved']},
            'findings': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 16, 'uniqueItems': True},
        }, 'state', 'findings'),
        example='{"tool":"submit_research_answer","args":{"state":"unresolved","findings":[]}}',
        description="End only the current frozen target. answered/partial require exact saved/read findings; "
                    "unresolved requires findings=[] and creates no finding. No direct support means unresolved, "
                    "not a nearby fact or a claim about the whole document. The application records actual attempts, "
                    "advances to the next target, and checks the parent report separately. A declaration is not semantic approval.",
    ),
    "web_search": ToolContract(
        name="web_search",
        input_schema=object_schema({
            'query': {'type': 'string'},
        }, 'query'),
        example='{"tool":"web_search","args":{"query":"..."}}',
        description="search the web for ranked results",
        parallel_safe=True,
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
        parallel_safe=True,
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
        parallel_safe=True,
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
        }, 'id'),
        example='{"tool":"knowledge_read","args":{"id":"<note id>"}}',
        description=("read a structured object and its pinned revision, conditions, provenance and references; "
                     "also accepts legacy note ids. " + knowledge_read_id_guidance()),
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


def _question_contract(contract: ToolContract) -> ToolContract:
    from codey.research.questions import QUESTION_BOUND_TOOLS

    if contract.name not in QUESTION_BOUND_TOOLS:
        return contract
    schema = copy.deepcopy(dict(contract.input_schema))
    schema["properties"]["question_ref"] = {"type": "string", "pattern": "^q[1-8]$"}
    example = json.loads(contract.example)
    example["args"]["question_ref"] = "q1"
    return replace(contract, input_schema=schema, example=json.dumps(example, separators=(",", ":")),
        description=contract.description + " Select question_ref=qN explicitly to bind this action to its registered question.")


TOOL_CONTRACTS = {name: _question_contract(contract) for name, contract in TOOL_CONTRACTS.items()}


def question_bound_spec(spec: Any, *, required: bool = False) -> Any:
    """One question argument contract shared by ordinary and strict research."""
    from codey.research.questions import QUESTION_BOUND_TOOLS

    if spec.name not in QUESTION_BOUND_TOOLS:
        return spec
    if "question_ref" in dict(spec.parameters):
        return replace(spec, required=tuple(dict.fromkeys((*spec.required, "question_ref")))) if required else spec
    parameters = {**dict(spec.parameters), "question_ref": {"type": "string", "pattern": "^q[1-8]$"}}
    keys = tuple(dict.fromkeys((*spec.required, "question_ref"))) if required else spec.required
    examples = []
    for text in spec.json_examples:
        example = json.loads(text)
        example["args"]["question_ref"] = "q1"
        examples.append(json.dumps(example, separators=(",", ":")))
    return replace(spec, parameters=tuple(parameters.items()), required=keys, json_examples=tuple(examples),
        description=spec.description + " Select question_ref=qN explicitly to bind this action to its registered question.")


def done_spec_for_purpose(spec: Any, purpose: str, *, task: str = "", questions: tuple[str, ...] = ()) -> Any:
    """Select one complete done contract before freezing, never patch pieces later."""
    example: dict[str, Any]
    if purpose == "ordinary":
        return spec
    if purpose == "evidence":
        schema = object_schema({"summary": {"type": "string"}}, "summary")
        example = {"tool": "done", "args": {"summary": "Saved findings and remaining evidence gaps"}}
        description = ("End evidence investigation with a brief receipt of saved findings and specific remaining gaps. "
                       "The parent owns final synthesis; do not write its report in this phase.")
    elif purpose == "report":
        coverage = dict(report_sections_schema()["properties"])["coverage"]
        coverage["items"]["properties"].pop("answer")
        coverage["items"]["required"] = [key for key in coverage["items"]["required"]
                                           if key not in {"answer", "investigations"}]
        if questions:
            coverage["items"]["properties"]["question_ref"]["enum"] = [
                f"q{index}" for index in range(1, len(questions) + 1)]
            coverage.update(minItems=len(questions), maxItems=len(questions))
        if task:
            from codey.research.task_requirements import coverage_requirements

            refs = [row["task_ref"] for row in coverage_requirements(task)]
            coverage["items"]["properties"]["task_refs"].update(
                items={"type": "string", **({"enum": refs} if refs else {})}, maxItems=len(refs))
            coverage["items"]["required"].append("task_refs")
        schema = object_schema({"report_sections": object_schema({"coverage": coverage}, "coverage")}, "report_sections")
        example = {"tool": "done", "args": {"report_sections": {
            "coverage": [{key: value for key, value in row.items() if key not in {"answer", "investigations"}}
                         for row in STRUCTURED_REPORT_ARGS["report_sections"]["coverage"]]}}}
        if task:
            example["args"]["report_sections"]["coverage"] = [
                {**row, "task_refs": refs} for row in example["args"]["report_sections"]["coverage"]]
        if questions:
            example["args"]["report_sections"]["coverage"] += [
                {"question_ref": f"q{index}", "state": "not_investigated", "findings": [],
                 **({"task_refs": []} if task else {})}
                for index in range(2, len(questions) + 1)]
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
    return replace(spec, parameters=tuple(schema["properties"].items()), required=tuple(schema["required"]),
                   json_examples=(json.dumps(example, separators=(",", ":")),), description=description)


def research_tool_names(*, include_source_search: bool = True) -> tuple[str, ...]:
    base = (
        "research_plan",
        "web_search",
        "open_url",
        "knowledge_search",
        "knowledge_read",
        "save_finding",
        "save_inference",
        "knowledge_write",
        "knowledge_link",
        "knowledge_commit",
        "done",
    )
    if not include_source_search:
        return base
    # source_search is inserted after open_url to match the system prompt order
    return (
        "research_plan",
        "web_search",
        "open_url",
        "source_search",
        "knowledge_search",
        "knowledge_read",
        "save_finding",
        "save_inference",
        "knowledge_write",
        "knowledge_link",
        "knowledge_commit",
        "done",
    )


def render_research_tool_contract_text(*, include_source_search: bool = True) -> str:
    lines: list[str] = []
    for name in research_tool_names(include_source_search=include_source_search):
        contract = TOOL_CONTRACTS[name]
        example = contract.example
        description = contract.description
        if description:
            lines.append(f"- {example}  {description}")
        else:
            lines.append(f"- {example}")
    return "\n".join(lines)


def research_tool_contract_hash(*, include_source_search: bool = True) -> str:
    from codey.toolchain.tool_prompt import model_visible_contract_hash

    return model_visible_contract_hash(
        "research_tool_contract",
        render_research_tool_contract_text(include_source_search=include_source_search),
    )


def openai_parameters_schema(tool: str) -> dict[str, Any]:
    contract = TOOL_CONTRACTS.get(tool)
    return copy.deepcopy(dict(contract.input_schema)) if contract else {
        "type": "object", "properties": {}, "additionalProperties": False}


def render_openai_tools(*, include_source_search: bool = True) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for name in research_tool_names(include_source_search=include_source_search):
        contract = TOOL_CONTRACTS[name]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": contract.description or contract.example,
                    "parameters": openai_parameters_schema(name),
                },
            }
        )
    def tool_name(item: dict[str, object]) -> str:
        function = item.get("function")
        return str(function.get("name") or "") if isinstance(function, dict) else ""

    tools.sort(key=tool_name)
    return tools


def validate_tool_args(tool: str, args: dict[str, Any]) -> ContractResult:
    """Direct callers use the same ToolSpec checker as the shared kernel."""
    from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec

    spec = spec_for_tool(tool)
    if spec is None:
        return ContractResult(False, error=f"unknown tool: {tool}", error_kind=PROTOCOL_UNKNOWN_TOOL)
    error = validate_args_with_spec(spec, args)
    return ContractResult(not error, args=dict(args) if not error else {}, error=error,
                          error_kind=PROTOCOL_INVALID_ARGS if error else "")


def tool_example(tool: str) -> str:
    # Single source: the static Tools: block and all dynamic repair /
    # controller allowed-action examples render from ToolContract examples.
    # Unknown tools fall back to the web_search shape (explicit repair policy).
    contract = TOOL_CONTRACTS.get(tool)
    if contract is not None:
        return contract.example
    return TOOL_CONTRACTS["web_search"].example


