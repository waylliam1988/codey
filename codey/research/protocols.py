"""JSON tool protocol for Research."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from codey.models import Control, ToolCall, ToolPlan, ToolResult

MAX_CALLS_PER_TURN = 4
_TOOL_ALIASES = {
    "web_search": ("web_search", "search"),
    "open_url": ("open_url", "open", "fetch", "read_url"),
    "knowledge_search": ("knowledge_search", "recall", "memory_search", "vault_search"),
    "knowledge_read": ("knowledge_read", "knowledge_note", "vault_read", "note_read"),
    "knowledge_write": ("knowledge_write", "note_write", "save_note", "write_note"),
    "knowledge_link": ("knowledge_link", "note_link", "link"),
    "done": ("done", "answer", "finish"),
}
_ALIAS_TO_TOOL = {alias: name for name, aliases in _TOOL_ALIASES.items() for alias in aliases}


class ProtocolCodec(Protocol):
    def system_prompt(self) -> str: ...
    def repair_prompt(self) -> str: ...
    def parse(self, text: str) -> ToolPlan: ...
    def format_results(self, results: list[ToolResult]) -> str: ...


@dataclass(frozen=True)
class JsonToolCodec:
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def repair_prompt(self) -> str:
        return (
            "Your last reply was not a valid tool call. Reply with exactly one "
            "JSON object and nothing else, for example:\n"
            '{"tool":"web_search","args":{"query":"..."}}\n'
            'or {"tool":"done","args":{"answer":"..."}}'
        )

    def parse(self, text: str) -> ToolPlan:
        objects = _extract_json_objects(text or "")
        if not objects:
            return ToolPlan(calls=[], control=None, protocol_error="no JSON tool call found")
        calls: list[ToolCall] = []
        control: Control | None = None
        for obj in objects:
            name = str(obj.get("tool") or obj.get("name") or "").strip().lower()
            runtime = _ALIAS_TO_TOOL.get(name)
            if runtime is None:
                continue
            args = obj.get("args")
            if not isinstance(args, dict):
                args = {k: v for k, v in obj.items() if k not in ("tool", "name")}
            if runtime == "done":
                body = str(args.get("answer") or args.get("summary") or args.get("text") or "")
                control = Control("done", body)
            else:
                calls.append(ToolCall(runtime, args))
        if not calls and control is None:
            return ToolPlan(calls=[], control=None, protocol_error="no known tool in reply")
        return ToolPlan(calls=calls[:MAX_CALLS_PER_TURN], control=control)

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
            "NEEDS_OPEN, call open_url for that URL before trying knowledge_write again."
        )


def _result_label(call: ToolCall) -> str:
    for key in ("query", "url", "id", "title", "src"):
        value = call.args.get(key)
        if value:
            return f'{call.name} "{value}"'
    return call.name


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
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


_SYSTEM_PROMPT = """You are CodeyResearch, a local research agent. You investigate a question on \
the live web, then save what you learn into a local Markdown knowledge library so it can be \
reused and audited later. You never invent facts.

Answer ONLY with JSON tool calls. No prose outside JSON. One JSON object per action.

Tools:
- {"tool":"web_search","args":{"query":"..."}}  search the web for ranked results
- {"tool":"open_url","args":{"url":"https://...","offset":0,"limit":6000}}  read a page's text
- {"tool":"knowledge_search","args":{"query":"..."}}  search your existing local notes FIRST
- {"tool":"knowledge_read","args":{"id":"<note id>"}}  read one existing note in full
- {"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...","tags":["..."],"sources":["https://..."],"evidence":[{"claim":"...","source_url":"https://...","excerpt":"...","stance":"supports"}],"confidence":0.6,"valid_until":"2026-12-31","status":"active"}}
- {"tool":"knowledge_link","args":{"src":"<note id>","dst":"<note id or exact title>","kind":"supports"}}
- {"tool":"done","args":{"answer":"<the full human-readable report>"}}

Note types (choose the right one; never mislabel):
- source: a web page you read (put its url in sources, and retrieved date context in body)
- fact: a verifiable claim. MUST include at least one source. Do not write a guess as a fact.
- hypothesis: your inference or expectation. It is NOT a fact. Label it as a hypothesis.
- conclusion: an actionable takeaway derived from facts + hypotheses.
- question: something still open that needs more research.
- synthesis: a full human-readable report for one research run.

Discipline:
- Start by calling knowledge_search to see what you already know.
- A web_search result is not evidence yet. After web_search, call open_url on useful result URLs before knowledge_write.
- Prefer 2+ independent sources before writing a fact.
- Every fact/conclusion note must cite sources.
- The final report may cite or name only pages you opened in this run, or grounded source notes you read.
- The final report must use these sections: 结论, 关键证据, 反证与限制, 来源质量, 搜索覆盖, 来源.
- Every cited number in the report must appear in 来源, and every 来源 URL must be a page you opened in this run.
- If no strong counter-evidence exists, write "未找到强反证" and explain what you searched that would have falsified the conclusion.
- Keep notes small and single-topic. Link related notes.

Be efficient: a handful of good searches and reads beat many shallow ones. When you have \
enough, save the important findings as notes, link them, then call done."""
