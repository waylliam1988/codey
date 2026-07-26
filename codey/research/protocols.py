"""JSON tool protocol for Research."""

from __future__ import annotations

import json
from typing import Any, Protocol

from codey.models import Control, ToolCall, ToolPlan, ToolResult
from codey.research.protocol_diagnostics import classify_no_json_reply
from codey.research.tool_contract import (
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_TOO_MANY_TOOLS,
    PROTOCOL_UNKNOWN_TOOL,
    validate_tool_args,
)

MAX_CALLS_PER_TURN = 1
_TOOL_ALIASES = {
    "web_search": ("web_search", "search"),
    "open_url": ("open_url", "open", "fetch", "read_url"),
    "source_search": ("source_search", "search_source", "find_in_source"),
    "knowledge_search": ("knowledge_search", "recall", "memory_search", "vault_search"),
    "knowledge_read": ("knowledge_read", "knowledge_note", "vault_read", "note_read"),
    "knowledge_write": ("knowledge_write", "note_write", "save_note", "write_note"),
    "knowledge_link": ("knowledge_link", "note_link", "link"),
    "done": ("done", "answer", "finish"),
}


def _alias_to_tool(include_source_search: bool = True) -> dict[str, str]:
    aliases = dict(_TOOL_ALIASES)
    if not include_source_search:
        aliases.pop("source_search", None)
    return {alias: name for name, names in aliases.items() for alias in names}


def canonical_tool_name(name: object, *, include_source_search: bool = True) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    return _alias_to_tool(include_source_search).get(raw, "")


class ProtocolCodec(Protocol):
    def system_prompt(self) -> str: ...
    def repair_prompt(self) -> str: ...
    def parse(self, text: str) -> ToolPlan: ...
    def format_results(self, results: list[ToolResult]) -> str: ...


class JsonToolCodec:
    def __init__(self, include_source_search: bool = True) -> None:
        self.include_source_search = bool(include_source_search)

    def system_prompt(self) -> str:
        return _system_prompt(self.include_source_search)

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
            "Do not use this chat website's built-in web search, browsing, "
            "plugins, or outside knowledge. Use only local JSON tools."
        )

    def parse(self, text: str) -> ToolPlan:
        objects = extract_json_objects(text or "")
        if not objects:
            kind, message = classify_no_json_reply(text or "")
            return ToolPlan(calls=[], control=None, protocol_error=message, protocol_error_kind=kind)
        actions: list[tuple[str, str, dict[str, Any]]] = []
        alias_map = _alias_to_tool(self.include_source_search)
        for obj in objects:
            name = str(obj.get("tool") or obj.get("name") or "").strip().lower()
            if not name:
                continue
            runtime = alias_map.get(name)
            if runtime is None:
                actions.append(("unknown", name, obj))
                continue
            actions.append(("known", runtime, obj))
        if not actions:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error="no known tool in reply",
                protocol_error_kind=PROTOCOL_UNKNOWN_TOOL,
            )
        if len(actions) > MAX_CALLS_PER_TURN:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=(
                    f"too many JSON tool calls in one reply ({len(actions)}); "
                    "reply with exactly one JSON object"
                ),
                protocol_error_kind=PROTOCOL_TOO_MANY_TOOLS,
            )
        action_kind, runtime, obj = actions[0]
        if action_kind == "unknown":
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=f"unknown tool: {runtime}",
                protocol_error_kind=PROTOCOL_UNKNOWN_TOOL,
            )
        args = obj.get("args")
        if not isinstance(args, dict):
            args = {k: v for k, v in obj.items() if k not in ("tool", "name")}
        validated = validate_tool_args(runtime, args)
        if not validated.ok:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=validated.error,
                protocol_error_kind=validated.error_kind or PROTOCOL_INVALID_ARGS,
            )
        if runtime == "done":
            return ToolPlan(calls=[], control=Control("done", str(validated.args.get("answer") or "")))
        calls = [ToolCall(runtime, validated.args)]
        return ToolPlan(calls=calls, control=None)

    def format_results(self, results: list[ToolResult]) -> str:
        blocks: list[str] = []
        for result in results:
            label = _result_label(result.call)
            suffix = " (truncated)" if result.truncated else ""
            blocks.append(f"[result: {label}{suffix}]\n{result.output}".rstrip())
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


_extract_json_objects = extract_json_objects


_SYSTEM_PROMPT = """You are a local research agent. You investigate a question on \
the live web, then save what you learn into a local Markdown knowledge library so it can be \
reused and audited later. You never invent facts.

Answer ONLY with JSON tool calls. No prose outside JSON. One JSON object per action.

Tools:
- {"tool":"web_search","args":{"query":"..."}}  search the web for ranked results
- {"tool":"open_url","args":{"url":"https://...","offset":0,"limit":6000,"pages":"1-5"}}  read a page's text; for PDFs, pages selects bounded page ranges
- {"tool":"knowledge_search","args":{"query":"..."}}  search your existing local notes FIRST
- {"tool":"knowledge_read","args":{"id":"<note id>"}}  read one existing note in full
- {"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...","tags":["..."],"sources":["https://..."],"relations":[{"src":"war","dst":"helium supply","kind":"affects"}],"evidence":[{"claim":"...","source_url":"https://...","excerpt":"exact short text copied from open_url output","stance":"supports"}],"confidence":0.6,"valid_until":"2026-12-31","status":"active"}}  save small source/fact/hypothesis/conclusion/question notes; final reports must use done
- {"tool":"knowledge_link","args":{"src":"<note id>","dst":"<note id or exact title>","kind":"supports"}}
- {"tool":"done","args":{"answer":"<the full human-readable report>"}}

Note types (choose the right one; never mislabel):
- source: a web page you read (put its url in sources, and retrieved date context in body)
- fact: a verifiable claim. MUST include at least one source. Do not write a guess as a fact.
- hypothesis: your inference or expectation. It is NOT a fact. Label it as a hypothesis.
- conclusion: an actionable takeaway derived from facts + hypotheses.
- question: something still open that needs more research.
- synthesis: created by Codey after done passes quality review. Do not write synthesis with knowledge_write.

Discipline:
- Start by calling knowledge_search to see what you already know.
- A web_search result is not evidence yet. After web_search, call open_url on useful result URLs before knowledge_write.
- open_url can read text PDFs. For PDFs, pass pages like "1-5" or "4"; default is the first pages.
- Prefer 2+ independent sources before writing a fact.
- Every fact/conclusion note must cite sources.
- Evidence snippets must be exact short excerpts copied from open_url text. For PDF-specific evidence, include evidence.page when known. Do not paraphrase evidence.excerpt. If uncertain, omit the evidence field and Codey will attach a source excerpt from the opened URL.
- The final report may cite or name only pages you opened in this run, or grounded source notes you read.
- The final report must use these sections: 结论, 关键证据, 反证与限制, 来源质量, 搜索覆盖, 来源.
- Every cited number in the report must appear in 来源, and every 来源 URL must be a page you opened in this run.
- Cite PDF page evidence as [1 p.4] when a claim depends on a specific page.
- Every 来源 citation must also have at least one saved knowledge_write evidence snippet from that opened page before done.
- If no strong counter-evidence exists, write "未找到强反证" and explain what you searched that would have falsified the conclusion.
- Keep notes small and single-topic. Link related notes.
- tags should be 2-5 short lowercase concept nouns (e.g. "helium supply", "war"), not sentences.
- relations declare concept-to-concept links the note's evidence supports, as {"src":...,"dst":...,"kind":affects/uses/causes/part_of/enables/relates}. Only declare relations the cited sources actually state; never declare a guessed relation.

Be efficient: a handful of good searches and reads beat many shallow ones. When you have \
enough, save the important findings as notes, link them, then call done."""


def _tool_names(include_source_search: bool) -> str:
    names = "web_search/open_url/knowledge_search/knowledge_read/knowledge_write/knowledge_link"
    if include_source_search:
        names += "/source_search"
    return names


def _hard_boundary(include_source_search: bool) -> str:
    return (
        "Research hard boundary:\n"
        "- Reply only with one JSON tool call. Do not write the research answer directly.\n"
        "- Choose exactly one tool. If you need another action, wait for the next local tool result first.\n"
        "- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.\n"
        f"- Use only these local JSON tools for web and knowledge access: {_tool_names(include_source_search)}.\n"
        "- Tool outputs are the only evidence."
    )


def _system_prompt(include_source_search: bool) -> str:
    if not include_source_search:
        return _hard_boundary(False) + "\n\n" + _SYSTEM_PROMPT
    tool_needle = (
        '- {"tool":"open_url","args":{"url":"https://...",'
        '"offset":0,"limit":6000,"pages":"1-5"}}  read a page\'s text; '
        "for PDFs, pages selects bounded page ranges\n"
    )
    tool_insert = (
        tool_needle
        + '- {"tool":"source_search","args":{"url":"https://...",'
        '"query":"...","limit":6}}  search within a source already opened with '
        "open_url; returns locators, offsets, and PDF pages\n"
    )
    prompt = _SYSTEM_PROMPT.replace(tool_needle, tool_insert)
    discipline_needle = (
        "- open_url can read text PDFs. For PDFs, pass pages like \"1-5\" or \"4\"; "
        "default is the first pages."
    )
    discipline_insert = (
        discipline_needle
        + "\n- source_search searches only inside already-opened sources. It returns "
        "locator previews, not evidence. For HTML, open the returned offset before "
        "citing. For PDF page-specific evidence, open_url pages=\"N\" before citing [n p.N]."
    )
    return _hard_boundary(True) + "\n\n" + prompt.replace(discipline_needle, discipline_insert)
