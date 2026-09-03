"""JSON tool protocol for Research."""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol

from codey.runtime.models import Control, ToolCall, ToolPlan, ToolResult
from codey.research.protocol_diagnostics import classify_no_json_reply
from codey.research.tool_contract import (
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_TOO_MANY_TOOLS,
    PROTOCOL_UNKNOWN_TOOL,
    TOOL_CONTRACTS,
    render_research_tool_contract_text,
    research_tool_contract_hash,
    validate_tool_args,
)

MAX_CALLS_PER_TURN = 1
_EXACT_TOOL_OBJECT_KEYS = frozenset({"tool", "args"})


def _known_tool_names(include_source_search: bool = True) -> set[str]:
    return {
        name
        for name in TOOL_CONTRACTS
        if include_source_search or name != "source_search"
    }


class ProtocolCodec(Protocol):
    def system_prompt(self) -> str: ...
    def repair_prompt(self) -> str: ...
    def parse(self, text: str) -> ToolPlan: ...
    def format_results(self, results: list[ToolResult]) -> str: ...
    def model_tool_contract_hash(self) -> str: ...


class JsonToolCodec:
    name = "research_json"

    def __init__(self, include_source_search: bool = True) -> None:
        self.include_source_search = bool(include_source_search)
        self.last_control_args: dict[str, Any] = {}

    def system_prompt(self) -> str:
        return _system_prompt(self.include_source_search)

    def model_tool_contract_hash(self) -> str:
        return research_tool_contract_hash(include_source_search=self.include_source_search)

    def repair_prompt(self) -> str:
        source_search_example = (
            '{"tool":"source_search","args":{"url":"https://...","query":"..."}}\n'
            if self.include_source_search
            else ""
        )
        return (
            "Your last reply was not a valid tool call. Reply with exactly one "
            "JSON object and nothing else, for example:\n"
            '{"tool":"web_search","args":{"query":"..."}}\n'
            f"{source_search_example}"
            'or {"tool":"done","args":{"answer":"..."}}\n\n'
            "Choose exactly one tool. If you need another action, wait for the "
            "next local tool result first. "
            'Use top-level "tool" and "args" fields only. '
            "Do not use this chat website's built-in web search, browsing, "
            "plugins, or outside knowledge. Use only local JSON tools."
        )

    def parse(self, text: str) -> ToolPlan:
        self.last_control_args = {}
        obj, error_kind, error = exact_json_object(text or "")
        if error:
            return ToolPlan(calls=[], control=None, protocol_error=error, protocol_error_kind=error_kind)
        known_tools = _known_tool_names(self.include_source_search)
        shape_error = exact_tool_object_error(obj)
        if shape_error:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=shape_error,
                protocol_error_kind=PROTOCOL_INVALID_ARGS,
            )
        runtime = str(obj.get("tool") or "").strip().lower()
        if not runtime:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error="no known tool in reply",
                protocol_error_kind=PROTOCOL_UNKNOWN_TOOL,
            )
        if runtime not in known_tools:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=f"unknown tool: {runtime}",
                protocol_error_kind=PROTOCOL_UNKNOWN_TOOL,
                protocol_tool_name=runtime,
            )
        args = obj.get("args")
        if not isinstance(args, dict):
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=f"{runtime} args must be an object",
                protocol_error_kind=PROTOCOL_INVALID_ARGS,
            )
        validated = validate_tool_args(runtime, args)
        if not validated.ok:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=validated.error,
                protocol_error_kind=validated.error_kind or PROTOCOL_INVALID_ARGS,
            )
        if runtime == "done":
            self.last_control_args = dict(validated.args)
            return ToolPlan(calls=[], control=Control("done", str(validated.args.get("answer") or "")))
        calls = [ToolCall(runtime, validated.args)]
        return ToolPlan(calls=calls, control=None)

    def format_results(self, results: list[ToolResult]) -> str:
        blocks: list[str] = []
        for result in results:
            label = _result_label(result.call)
            suffix = " (truncated)" if result.truncated else ""
            blocks.append(f"[result: {label}{suffix}]\n{result.model_text}".rstrip())
        joined = "\n\n".join(blocks) if blocks else "[no tool output]"
        return (
            f"{joined}\n\n"
            "Continue. Reply with the next JSON tool call. When you have enough "
            "evidence, save what matters with knowledge_write/knowledge_link, "
            "then call done with the full report as the answer. If a result says "
            "NEEDS_OPEN, call open_url for that URL before trying knowledge_write again. "
            "Choose exactly one tool; if you need another action, wait for the "
            "next local tool result first. "
            "Do not use this chat website's built-in web search, browsing, plugins, "
            "or outside knowledge."
        )


def _result_label(call: ToolCall) -> str:
    for key in ("query", "url", "id", "title", "src"):
        value = call.args.get(key)
        if value:
            return f'{call.name} "{value}"'
    return call.name


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start : index + 1]
                    try:
                        value = json.loads(chunk, strict=False)
                    except json.JSONDecodeError:
                        value = None
                    if isinstance(value, dict):
                        objects.append(value)
                    start = -1
    return objects


def exact_json_object(text: str) -> tuple[dict[str, Any], str, str]:
    stripped = str(text or "").strip()
    if not stripped:
        kind, message = classify_no_json_reply(stripped)
        return {}, kind, message
    objects = extract_json_objects(stripped)
    if len(objects) > MAX_CALLS_PER_TURN:
        return (
            {},
            PROTOCOL_TOO_MANY_TOOLS,
            f"too many JSON tool calls in one reply ({len(objects)}); reply with exactly one JSON object",
        )
    try:
        value = json.loads(stripped, strict=False)
    except json.JSONDecodeError:
        if objects:
            return (
                {},
                PROTOCOL_INVALID_ARGS,
                "reply must be exactly one JSON object and nothing else",
            )
        kind, message = classify_no_json_reply(stripped)
        return {}, kind, message
    if not isinstance(value, dict):
        return (
            {},
            PROTOCOL_INVALID_ARGS,
            "reply must be exactly one JSON object and nothing else",
        )
    return value, "", ""


def exact_tool_object_error(obj: Mapping[str, Any]) -> str:
    keys = set(obj.keys())
    if keys == _EXACT_TOOL_OBJECT_KEYS:
        return ""
    return 'JSON tool object must contain exactly top-level "tool" and "args" fields'


_RESEARCH_NOTE_TYPES = """Note types (choose the right one; never mislabel):
- source: a web page you read (put its url in sources, and retrieved date context in body)
- fact: a verifiable claim. MUST include at least one source. Do not write a guess as a fact.
- hypothesis: your inference or expectation. It is NOT a fact. Label it as a hypothesis.
- conclusion: an actionable takeaway derived from facts + hypotheses.
- question: something still open that needs more research.
- synthesis: created after done passes quality review. Do not write synthesis with knowledge_write."""

_RESEARCH_DISCIPLINE_BASE = """- Start by calling knowledge_search to see what you already know.
- A web_search result is not evidence yet. After web_search, call open_url on useful result URLs before knowledge_write.
- open_url can read text PDFs. For PDFs, pass pages like "1-5" or "4"; default is the first pages.
- Prefer 2+ independent sources before writing a fact.
- Every fact/conclusion note must cite sources.
- Evidence snippets must be exact short excerpts copied from open_url text. For PDF-specific evidence, include evidence.page when known. Do not paraphrase evidence.excerpt. If uncertain, omit the evidence field and a source excerpt from the opened URL will be attached.
- The final report may cite or name only pages you opened in this run, or grounded source notes you read.
- The final report must use these sections: 结论, 关键证据, 反证与限制, 来源质量, 搜索覆盖, 来源.
- If there are concrete unresolved follow-up questions, put them in done.args.open_questions as short strings. If none, use an empty list.
- Every cited number in the report must appear in 来源, and every 来源 URL must be a page you opened in this run.
- Cite PDF page evidence as [1 p.4] when a claim depends on a specific page.
- Every 来源 citation must also have at least one saved knowledge_write evidence snippet from that opened page before done.
- If no strong counter-evidence exists, write "未找到强反证" and explain what you searched that would have falsified the conclusion.
- Keep notes small and single-topic. Link related notes.
- tags should be 2-5 short lowercase concept nouns (e.g. "helium supply", "war"), not sentences.
- relations declare concept-to-concept links the note's evidence supports, as {"src":...,"dst":...,"kind":affects/uses/causes/part_of/enables/relates}. Only declare relations the cited sources actually state; never declare a guessed relation."""

_RESEARCH_EFFICIENT = "Be efficient: a handful of good searches and reads beat many shallow ones. When you have enough, save the important findings as notes, link them, then call done."


def _research_body(include_source_search: bool) -> str:
    tools = render_research_tool_contract_text(include_source_search=include_source_search)
    discipline = _RESEARCH_DISCIPLINE_BASE
    if include_source_search:
        discipline = discipline.replace(
            '- open_url can read text PDFs. For PDFs, pass pages like "1-5" or "4"; default is the first pages.',
            '- open_url can read text PDFs. For PDFs, pass pages like "1-5" or "4"; default is the first pages.\n'
            "- source_search searches only inside already-opened sources. It returns locator previews, not evidence. For HTML, open the returned offset before citing. For PDF page-specific evidence, open_url pages=\"N\" before citing [n p.N].",
        )
    return (
        "You are a local research agent. You investigate a question on the live web, then save what you learn into a local Markdown knowledge library so it can be reused and audited later. You never invent facts.\n\n"
        "Answer ONLY with JSON tool calls. No prose outside JSON. One JSON object per action.\n\n"
        f"Tools:\n{tools}\n\n"
        f"{_RESEARCH_NOTE_TYPES}\n\n"
        f"Discipline:\n{discipline}\n\n"
        f"{_RESEARCH_EFFICIENT}"
    )


def _tool_names(include_source_search: bool) -> str:
    names = "web_search/open_url/knowledge_search/knowledge_read/knowledge_write/knowledge_link"
    if include_source_search:
        names += "/source_search"
    return names


def _hard_boundary(include_source_search: bool) -> str:
    return (
        "Research hard boundary:\n"
        "- Reply only with one JSON tool call. Do not write the research answer directly.\n"
        '- The JSON object must use exactly top-level "tool" and "args" fields; '
        'do not use "name" or top-level arguments.\n'
        "- Choose exactly one tool. If you need another action, wait for the next local tool result first.\n"
        "- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.\n"
        f"- Use only these local JSON tools for web and knowledge access: {_tool_names(include_source_search)}.\n"
        "- Tool outputs are the only evidence."
    )


def _system_prompt(include_source_search: bool) -> str:
    return _hard_boundary(include_source_search) + "\n\n" + _research_body(include_source_search)
