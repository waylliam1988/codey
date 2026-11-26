"""Static declarations for research tools."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from codey.research.calculation_schema import calculation_schema, data_schema
from codey.research.knowledge_schema import (
    array_schema,
    comparison_schema,
    condition_array_schema,
    condition_map_schema,
    knowledge_reference_schema,
    mutation_schema,
    note_evidence_schema,
    object_schema,
)
from codey.types.json_value import JsonObject
from codey.types.research_answer import answer_fields

STRUCTURED_REPORT_ARGS: JsonObject = {"summary": "Supported conclusion [f1]", "report_sections": {
    "evidence": ["Saved finding with its conditions [f1]"],
    "counter": ["Observed limitation and its scope [f1]"],
    "coverage": [{"question_ref": "q1", "state": "partial", "answer": "Answer and remaining gap",
                  "findings": ["f1"], "investigations": []}]}}
STRUCTURED_REPORT_EXAMPLE = json.dumps(dict[str, object](tool="done", args=STRUCTURED_REPORT_ARGS), separators=(",", ":"))


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

TOOL_CONTRACTS = {
    "read_data": ToolContract("read_data", data_schema(),
        description="Parse an original eN evidence passage or a complete file already read in this task as quantities, CSV or JSON. "
                    "Select file data by its read_file path or result_ref. Inspect unwrap(result).data. "
                    "Use the outer read_data Result.result_ref when passing its data to calculate."),
    "calculate": ToolContract("calculate", calculation_schema(),
        description="Exact Decimal arithmetic over committed read_data/calculate receipts. Supply table for filtered CSV "
                    "or explicit {result_ref,pointer} operands and weights. "
                    "For a CSV in CodeMode: unwrap(await tools.read_file({path:inputs.path})); "
                    "const table=await tools.read_data({path:inputs.path,format:'csv'}); "
                    "const checked=unwrap(await tools.calculate({operation:'weighted_mean',table:{result_ref:table.result_ref,"
                    "value_column:'value',weight_column:'weight',where:{column:'cohort',equals:inputs.group}}})); "
                    "return {weighted_mean:checked.value,row_count:checked.inputs.length}; "
                    "The host selects original rows and keeps values aligned with their weights. For a frozen web passage, "
                    "read_data takes its eN evidence label. "
                    "Keep result_ref on the outer receipt. growth_rate uses [baseline,new]; percentage_points uses percentages. "
                    "Unit conversion, missing policy, precision and half-even rounding are explicit. Results remain semantically unreviewed. "
                    "Research reports can use the offered {{calc:cN}} slots."),
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
        name="submit_research_answer", input_schema=object_schema(dict(answer_fields()), "markdown", "state"),
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
            'knowledge_refs': array_schema(knowledge_reference_schema()),
        }, 'summary'),
        example='{"tool":"done","args":{"summary":"<the complete user-facing answer>"}}',
        description="summary is the complete user-facing answer. When citing retrieved knowledge, "
                    "knowledge_refs selects the exact versions read for this task.",
    ),
}
