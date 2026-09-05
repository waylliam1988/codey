"""Thin state-aware gate for Codey Research.

The controller is deliberately not a planner. It reads the per-run ledger,
exposes only currently reasonable tools, and rewrites stable local IDs into the
ordinary JSON tool arguments that the existing codec already validates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from codey.runtime.models import Control, ToolPlan, ToolResult
from codey.research.protocols import ProtocolCodec, exact_json_object, exact_tool_object_error
from codey.research.source_document import compact_pages
from codey.research.tool_contract import (
    PROTOCOL_DISALLOWED_TOOL,
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_UNKNOWN_TOOL,
    tool_example,
)
from codey.research.url_selection import source_candidate_skip_reason

CONTROLLER_DISPLAY_LIMIT = 8
FINAL_REPORT_SECTION_MARKERS = ("结论", "关键证据", "反证与限制", "来源质量", "搜索覆盖", "来源")


@dataclass(frozen=True)
class ControllerActionContract:
    name: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    lowers_to: str = ""


CONTROLLER_ACTION_CONTRACTS: tuple[ControllerActionContract, ...] = (
    ControllerActionContract(name="knowledge_search", required=("query",), lowers_to="knowledge_search"),
    ControllerActionContract(name="knowledge_read", required=("id",), lowers_to="knowledge_read"),
    ControllerActionContract(name="web_search", required=("query",), lowers_to="web_search"),
    ControllerActionContract(name="open_result", required=("result_id",), lowers_to="open_url"),
    ControllerActionContract(
        name="reopen_source",
        required=("source_id",),
        optional=("offset", "limit", "pages"),
        lowers_to="open_url",
    ),
    ControllerActionContract(name="open_hit", required=("hit_id",), lowers_to="open_url"),
    ControllerActionContract(
        name="source_search",
        required=("source_id", "query"),
        optional=("limit",),
        lowers_to="source_search",
    ),
    ControllerActionContract(
        name="knowledge_write",
        required=("type", "title", "body"),
        optional=(
            "id",
            "sources",
            "tags",
            "aliases",
            "relations",
            "evidence",
            "open_questions",
            "confidence",
            "retrieved_at",
            "valid_until",
            "status",
        ),
        lowers_to="knowledge_write",
    ),
    ControllerActionContract(
        name="knowledge_link",
        required=("src", "dst"),
        optional=("kind",),
        lowers_to="knowledge_link",
    ),
    ControllerActionContract(name="done", required=("answer",), optional=("open_questions",), lowers_to="done"),
)


def controller_action_names(*, include_source_search: bool = True) -> tuple[str, ...]:
    names = tuple(contract.name for contract in CONTROLLER_ACTION_CONTRACTS)
    if include_source_search:
        return names
    return tuple(name for name in names if name != "source_search")


def render_controller_action_contract_text(*, include_source_search: bool = True) -> str:
    lines: list[str] = []
    for contract in CONTROLLER_ACTION_CONTRACTS:
        if contract.name == "source_search" and not include_source_search:
            continue
        required = ",".join(contract.required)
        optional = ",".join(contract.optional)
        lowers = f" -> {contract.lowers_to}" if contract.lowers_to and contract.lowers_to != contract.name else ""
        lines.append(f"{contract.name} required=[{required}] optional=[{optional}]{lowers}")
    return "\n".join(lines)


def controller_action_contract_hash(*, include_source_search: bool = True) -> str:
    """Hash the Research controller action contract visible to the model."""

    from codey.tool_prompt import model_visible_contract_hash

    return model_visible_contract_hash(
        "research_controller_action_contract",
        render_controller_action_contract_text(include_source_search=include_source_search),
    )


def controller_system_prompt(*, include_source_search: bool = True) -> str:
    tool_names = (
        "knowledge_search/knowledge_read/web_search/open_result/reopen_source/open_hit/"
        "knowledge_write/knowledge_link/done"
    )
    if include_source_search:
        tool_names = tool_names.replace("knowledge_write", "source_search/knowledge_write")
    source_search_line = (
        "\n- source_search is a locator inside an already-opened source, not evidence. "
        "Use open_hit on a returned hit_id before citing."
        if include_source_search
        else ""
    )
    return f"""Research hard boundary:
- Reply only with one JSON tool call. Do not write the research answer directly.
- Choose exactly one tool. If you need another action, wait for the next local tool result first.
- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.
- Use only these local JSON actions for web and knowledge access: {tool_names}.
- The JSON object must use exactly top-level "tool" and "args" fields; do not use "name" or top-level arguments.
- Tool outputs are the only evidence.

You are a local research agent. You investigate a question using only local
JSON tools, then save what you learn into a local Markdown knowledge
library so it can be reused and audited later. You never invent facts.

A local controller will append a "Research controller current allowed actions" block every
turn. Use only the tools and exact JSON shapes shown in that block.

Research discipline:
- A web_search result is not evidence. Use open_result on useful results before knowledge_write.
- Use reopen_source for another offset/page of an already-opened source.
- Prefer result_id/source_id/hit_id over hand-copying URLs when an ID is available.{source_search_line}
- Evidence snippets must be exact short excerpts copied from opened source text.
- Note tags should be 2-5 short lowercase concept nouns (e.g. "helium supply", "war"), not sentences.
- knowledge_write relations declare concept-to-concept links the note's evidence supports, as {{"src":...,"dst":...,"kind":affects/uses/causes/part_of/enables/relates}}. Only declare relations the cited sources actually state; never declare a guessed relation.
- Final reports must use done, with these sections: 结论, 关键证据, 反证与限制, 来源质量, 搜索覆盖, 来源.
- Every cited source URL in the final report must be opened in this run and have saved evidence.
- If no strong counter-evidence exists, write "未找到强反证" and explain what was searched."""


@dataclass(frozen=True)
class OpenTarget:
    url: str
    offset: int = 0
    limit: int = 6000
    pages: str = ""


@dataclass(frozen=True)
class ResearchControlState:
    allowed_tools: tuple[str, ...]
    result_urls: dict[str, str] = field(default_factory=dict)
    priority_result_ids: tuple[str, ...] = ()
    source_urls: dict[str, str] = field(default_factory=dict)
    hit_targets: dict[str, OpenTarget] = field(default_factory=dict)
    result_lines: tuple[str, ...] = ()
    source_lines: tuple[str, ...] = ()
    hit_lines: tuple[str, ...] = ()
    citable_source_lines: tuple[str, ...] = ()
    noncitable_source_lines: tuple[str, ...] = ()
    evidence_count: int = 0
    note_count: int = 0
    done_escape: bool = False
    finish_required: bool = False


class ResearchController:
    def __init__(self, *, include_source_search: bool = True) -> None:
        self.include_source_search = bool(include_source_search)
        self._result_ids_by_url: dict[str, str] = {}
        self._source_ids_by_url: dict[str, str] = {}
        self._source_urls_by_id: dict[str, str] = {}
        self._hit_ids_by_key: dict[str, str] = {}
        self._failed_priority_result_url_keys: set[str] = set()

    def build_state(self, tools: object, *, turn: int, max_turns: int) -> ResearchControlState:
        ledger = tools.ledger
        result_rows = self._available_result_rows(self._result_rows(ledger))
        source_rows = self._source_rows(ledger)
        hit_rows = self._hit_rows(ledger)
        evidence_urls = _evidence_source_urls(ledger)
        evidence_count = len(ledger.evidence_items)
        note_count = len(getattr(tools, "created_ids", ())) + len(getattr(tools, "updated_ids", ()))
        grounded_count = len(getattr(tools, "grounded_ids", ()))
        activity = bool(ledger.searches or ledger.opened_sources)
        done_escape = evidence_count == 0 and activity and int(turn or 0) >= max(1, int(max_turns or 1) - 1)
        finish_required = _finish_required(turn=turn, max_turns=max_turns, evidence_count=evidence_count)
        priority_result_ids = _priority_connector_result_ids(result_rows, source_rows)

        if finish_required:
            allowed = ["done"]
            if source_rows:
                allowed.append("knowledge_write")
            if note_count >= 2 or (note_count >= 1 and grounded_count):
                allowed.append("knowledge_link")
        elif priority_result_ids:
            allowed = ["open_result"]
        else:
            allowed = ["knowledge_search", "knowledge_read", "web_search"]
            if result_rows:
                allowed.append("open_result")
            if source_rows:
                allowed.append("reopen_source")
            if hit_rows:
                allowed.append("open_hit")
            if source_rows and self.include_source_search:
                allowed.append("source_search")
            if source_rows:
                allowed.append("knowledge_write")
            if note_count >= 2 or (note_count >= 1 and grounded_count):
                allowed.append("knowledge_link")
            if evidence_count > 0 or done_escape:
                allowed.append("done")

        citable = tuple(row["line"] for row in source_rows if row["url"] in evidence_urls)
        noncitable = tuple(row["line"] for row in source_rows if row["url"] not in evidence_urls)
        visible_result_rows = (
            tuple(row for row in result_rows if row["id"] in priority_result_ids)
            if priority_result_ids
            else tuple(result_rows[-CONTROLLER_DISPLAY_LIMIT:])
        )
        return ResearchControlState(
            allowed_tools=tuple(dict.fromkeys(allowed)),
            result_urls={row["id"]: row["url"] for row in result_rows},
            priority_result_ids=priority_result_ids,
            source_urls=dict(self._source_urls_by_id),
            hit_targets={row["id"]: row["target"] for row in hit_rows},
            result_lines=tuple(row["line"] for row in visible_result_rows),
            source_lines=tuple(row["line"] for row in source_rows[-CONTROLLER_DISPLAY_LIMIT:]),
            hit_lines=tuple(row["line"] for row in hit_rows[-CONTROLLER_DISPLAY_LIMIT:]),
            citable_source_lines=citable[-CONTROLLER_DISPLAY_LIMIT:],
            noncitable_source_lines=noncitable[-CONTROLLER_DISPLAY_LIMIT:],
            evidence_count=evidence_count,
            note_count=note_count,
            done_escape=done_escape,
            finish_required=finish_required,
        )

    def parse_plan(self, codec: ProtocolCodec, reply: str, state: ResearchControlState) -> ToolPlan:
        obj, error_kind, error = exact_json_object(reply or "")
        if error:
            finish_answer = _finish_report_answer(reply, state)
            if finish_answer:
                return ToolPlan(calls=[], control=Control("done", finish_answer))
            return ToolPlan(calls=[], control=None, protocol_error=error, protocol_error_kind=error_kind)
        plan = self._parse_controller_object(codec, obj, state)
        if plan.protocol_error or (not plan.calls and plan.control is None):
            return plan
        return plan

    def append_block(self, message: str, state: ResearchControlState) -> str:
        return str(message or "").rstrip() + "\n\n" + render_control_block(state)

    def record_tool_outcome(
        self,
        state: ResearchControlState | None,
        call: object,
        model_text: str,
    ) -> None:
        if state is None or not state.priority_result_ids:
            return
        if str(getattr(call, "name", "") or "") != "open_url":
            return
        if not _open_failed(model_text):
            return
        args = getattr(call, "args", {})
        if not isinstance(args, dict):
            return
        url_key = _result_url_key(args.get("url"))
        if not url_key:
            return
        priority_url_keys = {
            _result_url_key(state.result_urls.get(result_id, ""))
            for result_id in state.priority_result_ids
        }
        if url_key in priority_url_keys:
            self._failed_priority_result_url_keys.add(url_key)

    def _parse_controller_object(
        self,
        codec: ProtocolCodec,
        obj: dict[str, Any],
        state: ResearchControlState,
    ) -> ToolPlan:
        shape_error = exact_tool_object_error(obj)
        if shape_error:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=shape_error,
                protocol_error_kind=PROTOCOL_INVALID_ARGS,
            )
        raw_tool = str(obj.get("tool") or "").strip().lower()
        if not raw_tool:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error="no known tool in reply",
                protocol_error_kind=PROTOCOL_UNKNOWN_TOOL,
            )
        tool = controller_tool_name(
            raw_tool,
            include_source_search=self.include_source_search,
        )
        if not tool:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=f"unknown tool: {raw_tool}",
                protocol_error_kind=PROTOCOL_UNKNOWN_TOOL,
                protocol_tool_name=raw_tool,
            )
        if state.allowed_tools and tool not in state.allowed_tools:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=(
                    f"{tool} is not allowed by the current Research controller state; "
                    f"allowed tools: {', '.join(state.allowed_tools)}"
                ),
                protocol_error_kind=PROTOCOL_DISALLOWED_TOOL,
                protocol_tool_name=tool,
            )
        rewritten, error = compile_controller_action(obj, state, tool=tool)
        if error:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=error,
                protocol_error_kind=PROTOCOL_INVALID_ARGS,
            )
        return codec.parse(json.dumps(rewritten, ensure_ascii=False))

    def _result_rows(self, ledger: object) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for search in getattr(ledger, "searches", ()):
            for result in search.results:
                url = str(result.url or "").strip()
                if not url:
                    continue
                if source_candidate_skip_reason(url):
                    continue
                rid = _stable_id(self._result_ids_by_url, url, "r")
                rows.append({
                    "id": rid,
                    "title": str(result.title or ""),
                    "url": url,
                    "snippet": str(result.snippet or ""),
                    "line": _result_line(rid, str(result.title or ""), url, str(result.snippet or "")),
                })
        return _recent_unique_rows(rows)

    def _available_result_rows(self, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        if not self._failed_priority_result_url_keys:
            return rows
        return [
            row for row in rows
            if _result_url_key(row.get("url")) not in self._failed_priority_result_url_keys
        ]

    def _source_rows(self, ledger: object) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for source in getattr(ledger, "opened_sources", ()):
            final_url = str(source.final_url or source.requested_url or "").strip()
            if not final_url:
                continue
            sid = _stable_id(self._source_ids_by_url, final_url, "s")
            self._source_urls_by_id[sid] = final_url
            requested = str(source.requested_url or "").strip()
            if requested:
                self._source_ids_by_url.setdefault(requested, sid)
            line = _source_line(
                sid,
                str(source.title or ""),
                final_url,
                str(source.content_kind or "html"),
                compact_pages(source.pages_read),
            )
            rows.append({"id": sid, "url": final_url, "line": line})
        return _recent_unique_rows(rows)

    def _hit_rows(self, ledger: object) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for search in getattr(ledger, "source_searches", ()):
            source_url = str(search.get("source_url") or "").strip()
            if not source_url:
                continue
            sid = self._source_ids_by_url.get(source_url) or _stable_id(self._source_ids_by_url, source_url, "s")
            self._source_urls_by_id.setdefault(sid, source_url)
            for hit in search.get("hits") or ():
                page = _as_optional_int(hit.get("page"))
                offset = max(0, _as_int(hit.get("offset"), 0))
                snippet = str(hit.get("snippet") or "")
                key = f"{source_url}\0{page or ''}\0{offset}\0{snippet[:120]}"
                hid = _stable_id(self._hit_ids_by_key, key, "h")
                target = (
                    OpenTarget(url=source_url, pages=str(page), offset=0, limit=6000)
                    if page is not None
                    else OpenTarget(url=source_url, offset=offset, limit=6000)
                )
                locator = f"{sid} p.{page}" if page is not None else f"{sid} offset {offset}"
                rows.append({
                    "id": hid,
                    "url": source_url,
                    "target": target,
                    "line": f"{hid}: {locator} - {_clip(snippet, 180)}",
                })
        return _recent_unique_rows(rows)


def compile_controller_action(
    obj: dict[str, Any],
    state: ResearchControlState,
    *,
    tool: str,
) -> tuple[dict[str, Any], str]:
    rewritten = dict(obj)
    shape_error = exact_tool_object_error(rewritten)
    if shape_error:
        return rewritten, shape_error
    args = rewritten.get("args")
    if not isinstance(args, dict):
        return rewritten, f"{tool} args must be an object"
    args = dict(args)
    if tool == "open_result":
        output_args, error = _compile_open_result_args(args, state)
        if error:
            return rewritten, error
        return {"tool": "open_url", "args": output_args}, ""
    if tool == "reopen_source":
        output_args, error = _compile_reopen_source_args(args, state)
        if error:
            return rewritten, error
        return {"tool": "open_url", "args": output_args}, ""
    if tool == "open_hit":
        output_args, error = _compile_open_hit_args(args, state)
        if error:
            return rewritten, error
        return {"tool": "open_url", "args": output_args}, ""
    if tool == "source_search":
        error = _compile_source_search_args(args, state)
        if error:
            return rewritten, error
    elif tool == "knowledge_write":
        error = _rewrite_knowledge_write_args(args, state)
        if error:
            return rewritten, error
    elif not tool:
        return rewritten, "unknown controller action"
    rewritten["tool"] = tool
    rewritten["args"] = args
    return rewritten, ""


def render_control_block(state: ResearchControlState) -> str:
    lines = [
        "Research controller current allowed actions:",
        f"- Allowed tools this turn: {', '.join(state.allowed_tools)}",
        "- Reply with exactly one JSON object using only the allowed tools below.",
        '- Use top-level "tool" and "args" fields exactly; do not use "name" or top-level arguments.',
        "- Tools not listed here are forbidden this turn, even if they appeared earlier.",
        "- Prefer result_id/source_id/hit_id over hand-copying URLs when an ID is available.",
        "- Use open_result for search results and reopen_source for already-opened source pages/offsets.",
        f"- Saved evidence items: {state.evidence_count}; saved/updated notes: {state.note_count}.",
        "- In done.answer, cite and list only evidence-backed source URLs. Opened-only sources are not citable yet.",
    ]
    if "source_search" in state.allowed_tools or "open_hit" in state.allowed_tools:
        lines.append(
            "- Use source_search with source_id to search inside an opened source; "
            "source_search hits are not evidence until opened with open_hit."
        )
    if state.done_escape and state.evidence_count == 0:
        lines.append(
            "- done is allowed only to report insufficient/no citable evidence and what was searched."
        )
    if state.finish_required:
        lines.append(
            "- Finish now: call done with the evidence-backed report, or save one missing opened-source "
            "evidence item with knowledge_write before done."
        )
    lines.extend(("", "Allowed JSON shapes for this turn (choose exactly one):"))
    for tool in state.allowed_tools:
        for example in _tool_examples(tool, state):
            lines.append(f"- {example}")
    if state.result_lines:
        if state.priority_result_ids:
            lines.extend((
                "",
                "Priority source results you must open before ordinary web results:",
                f"- Use one of these result_id values first: {', '.join(state.priority_result_ids)}",
            ))
        else:
            lines.extend(("", "Search results you may open:"))
        lines.extend(f"- {line}" for line in state.result_lines)
    if state.source_lines:
        lines.extend(("", "Opened sources you may inspect or reopen:"))
        lines.extend(f"- {line}" for line in state.source_lines)
    if state.hit_lines:
        lines.extend(("", "source_search hits you may open before citing:"))
        lines.extend(f"- {line}" for line in state.hit_lines)
    if state.citable_source_lines:
        lines.extend(("", "Evidence-backed sources allowed in final 来源:"))
        lines.extend(f"- {line}" for line in state.citable_source_lines)
    if state.noncitable_source_lines:
        lines.extend(("", "Opened but not citable in final 来源 until knowledge_write saves evidence:"))
        lines.extend(f"- {line}" for line in state.noncitable_source_lines)
    lines.extend(("", "Do not call tools outside the allowed list. Do not output multiple JSON objects."))
    return "\n".join(lines)


def _finish_required(*, turn: int, max_turns: int, evidence_count: int) -> bool:
    if int(evidence_count or 0) <= 0:
        return False
    return int(turn or 0) >= max(1, int(max_turns or 1) - 2)


def _finish_report_answer(reply: str, state: ResearchControlState) -> str:
    if not state.finish_required or "done" not in state.allowed_tools:
        return ""
    text = str(reply or "").strip()
    if len(text) < 80:
        return ""
    if "结论" not in text or "来源" not in text:
        return ""
    marker_count = sum(1 for marker in FINAL_REPORT_SECTION_MARKERS if marker in text)
    return text if marker_count >= 3 else ""


def _priority_connector_result_ids(result_rows: list[dict[str, str]], source_rows: list[dict[str, str]]) -> tuple[str, ...]:
    if any(_is_connector_source_url(row["url"]) for row in source_rows):
        return ()
    ids: list[str] = []
    for row in result_rows:
        if _is_connector_source_url(row["url"]):
            ids.append(str(row["id"]))
        if len(ids) >= 2:
            break
    return tuple(ids)


def _is_connector_source_url(url: str) -> bool:
    if source_candidate_skip_reason(url):
        return False
    parsed = _parsed_url(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "pubmed.ncbi.nlm.nih.gov":
        return True
    return host == "arxiv.org" and parsed.path.startswith("/abs/")


def _parsed_url(url: str):
    try:
        return urlparse(str(url or "").strip())
    except ValueError:
        return urlparse("")


def _result_url_key(url: object) -> str:
    parsed = _parsed_url(str(url or ""))
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    query = ("?" + parsed.query) if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{query}" if host else ""


def _open_failed(model_text: str) -> bool:
    return str(model_text or "").startswith(("ERROR:", "SKIPPED:", "NEEDS_OPEN:"))


def format_controller_results(results: list[ToolResult]) -> str:
    blocks: list[str] = []
    for result in results:
        label = _controller_result_label(result.call)
        suffix = " (truncated)" if result.truncated else ""
        blocks.append(f"[result: {label}{suffix}]\n{result.model_text}".rstrip())
    joined = "\n\n".join(blocks) if blocks else "[no tool output]"
    return (
        f"{joined}\n\n"
        "Continue. Reply with the next JSON tool call from the current allowed-actions block. "
        "When you have enough evidence, save what matters with knowledge_write/knowledge_link, "
        "then call done with the full report as the answer. If a result says NEEDS_OPEN, "
        "open the relevant source through the current open_result/reopen_source/open_hit action "
        "before trying knowledge_write again. Choose exactly one tool; if you need another action, "
        "wait for the next local tool result first. Do not use this chat website's built-in web "
        "search, browsing, plugins, or outside knowledge."
    )


def _compile_open_result_args(args: dict[str, Any], state: ResearchControlState) -> tuple[dict[str, Any], str]:
    result_id = _normalized_id(args.get("result_id"))
    if not result_id:
        return {}, "open_result missing required arg 'result_id'"
    if state.priority_result_ids and result_id not in state.priority_result_ids:
        return {}, (
            "open_result must use a priority source result_id before ordinary web results: "
            + ", ".join(state.priority_result_ids)
        )
    url = state.result_urls.get(result_id, "")
    if not url:
        return {}, f"unknown result_id: {result_id}"
    return {"url": url}, ""


def _compile_reopen_source_args(args: dict[str, Any], state: ResearchControlState) -> tuple[dict[str, Any], str]:
    source_id = _normalized_id(args.get("source_id"))
    if not source_id:
        return {}, "reopen_source missing required arg 'source_id'"
    url = state.source_urls.get(source_id, "")
    if not url:
        return {}, f"unknown source_id: {source_id}"
    output: dict[str, Any] = {"url": url}
    for key in ("offset", "limit", "pages"):
        if key in args:
            output[key] = args[key]
    return output, ""


def _compile_open_hit_args(args: dict[str, Any], state: ResearchControlState) -> tuple[dict[str, Any], str]:
    hit_id = _normalized_id(args.get("hit_id"))
    if not hit_id:
        return {}, "open_hit missing required arg 'hit_id'"
    target = state.hit_targets.get(hit_id)
    if target is None:
        return {}, f"unknown hit_id: {hit_id}"
    return {
        "url": target.url,
        "offset": target.offset,
        "limit": target.limit,
        "pages": target.pages,
    }, ""


def _compile_source_search_args(args: dict[str, Any], state: ResearchControlState) -> str:
    source_id = _normalized_id(args.get("source_id"))
    if not source_id:
        return "source_search missing required arg 'source_id'"
    url = state.source_urls.get(source_id, "")
    if not url:
        return f"unknown source_id: {source_id}"
    args["url"] = url
    return ""


def _rewrite_knowledge_write_args(args: dict[str, Any], state: ResearchControlState) -> str:
    if "sources" in args:
        sources, error = _rewrite_sources(args["sources"], state)
        if error:
            return error
        args["sources"] = sources
    if "evidence" in args:
        evidence, error = _rewrite_evidence(args["evidence"], state)
        if error:
            return error
        args["evidence"] = evidence
    return ""


def _rewrite_sources(value: object, state: ResearchControlState) -> tuple[list[object], str]:
    items = value if isinstance(value, list) else [value]
    rewritten = []
    for item in items:
        source_id = _normalized_id(item)
        if source_id in state.source_urls:
            rewritten.append(state.source_urls[source_id])
        elif _looks_like_source_id(source_id):
            return [], f"unknown source_id: {source_id}"
        else:
            rewritten.append(item)
    return rewritten, ""


def _rewrite_evidence(value: object, state: ResearchControlState) -> tuple[object, str]:
    if isinstance(value, dict):
        item, error = _rewrite_evidence_item(value, state)
        return item, error
    if isinstance(value, list):
        rewritten = []
        for raw in value:
            if not isinstance(raw, dict):
                rewritten.append(raw)
                continue
            item, error = _rewrite_evidence_item(raw, state)
            if error:
                return value, error
            rewritten.append(item)
        return rewritten, ""
    return value, ""


def _rewrite_evidence_item(value: dict[str, Any], state: ResearchControlState) -> tuple[dict[str, Any], str]:
    item = dict(value)
    source_id = _normalized_id(item.get("source_url") or item.get("source"))
    if source_id in state.source_urls:
        if "source_url" in item:
            item["source_url"] = state.source_urls[source_id]
        if "source" in item:
            item["source"] = state.source_urls[source_id]
    elif _looks_like_source_id(source_id):
        return item, f"unknown source_id: {source_id}"
    return item, ""


def _tool_examples(tool: str, state: ResearchControlState) -> tuple[str, ...]:
    if tool == "open_result":
        if state.priority_result_ids:
            rid = state.priority_result_ids[0]
            return (f'{{"tool":"open_result","args":{{"result_id":"{rid}"}}}}',)
        if state.result_urls:
            rid = next(iter(state.result_urls))
            return (f'{{"tool":"open_result","args":{{"result_id":"{rid}"}}}}',)
        return ('{"tool":"open_result","args":{"result_id":"r1"}}',)
    if tool == "reopen_source":
        if state.source_urls:
            sid = next(iter(state.source_urls))
            return (
                f'{{"tool":"reopen_source","args":{{"source_id":"{sid}","offset":0,"limit":6000,"pages":""}}}}',
            )
        return ('{"tool":"reopen_source","args":{"source_id":"s1","offset":0,"limit":6000,"pages":""}}',)
    if tool == "open_hit":
        if state.hit_targets:
            hid = next(iter(state.hit_targets))
            return (f'{{"tool":"open_hit","args":{{"hit_id":"{hid}"}}}}',)
        return ('{"tool":"open_hit","args":{"hit_id":"h1"}}',)
    if tool == "source_search":
        if state.source_urls:
            sid = next(iter(state.source_urls))
            return (f'{{"tool":"source_search","args":{{"source_id":"{sid}","query":"...","limit":6}}}}',)
        return (tool_example("source_search"),)
    if tool == "knowledge_write" and state.source_urls:
        sid = next(iter(state.source_urls))
        return (
            '{"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...",'
            '"tags":["short concept noun"],'
            f'"sources":["{sid}"],'
            '"relations":[{"src":"...","dst":"...","kind":"affects"}],'
            f'"evidence":{{"claim":"...","source_url":"{sid}",'
            '"excerpt":"exact short text from opened source","stance":"supports"}}}',
        )
    return (tool_example(tool),)


def controller_tool_example(tool: str, state: ResearchControlState) -> str:
    examples = _tool_examples(tool, state)
    return examples[0] if examples else tool_example("web_search")


def controller_tool_name(name: object, *, include_source_search: bool = True) -> str:
    raw = str(name or "").strip().lower()
    tools = set(controller_action_names(include_source_search=include_source_search))
    return raw if raw in tools else ""


def _controller_result_label(call: object) -> str:
    name = str(getattr(call, "name", "") or "")
    args = getattr(call, "args", {}) if call is not None else {}
    if not isinstance(args, dict):
        args = {}
    if name == "open_url":
        value = args.get("url")
        return f'opened_source "{value}"' if value else "opened_source"
    for key in ("query", "id", "title", "src"):
        value = args.get(key)
        if value:
            return f'{name} "{value}"'
    return name


def _result_line(result_id: str, title: str, url: str, snippet: str) -> str:
    return f"{result_id}: {title or url} - {url} - {_clip(snippet, 160)}"


def _source_line(source_id: str, title: str, url: str, kind: str, pages: str) -> str:
    meta = kind or "html"
    if pages:
        meta += f" pages {pages}"
    return f"{source_id}: {title or url} - {url} ({meta})"


def _stable_id(mapping: dict[str, str], key: str, prefix: str) -> str:
    existing = mapping.get(key)
    if existing:
        return existing
    value = f"{prefix}{len({item for item in mapping.values() if item.startswith(prefix)}) + 1}"
    mapping[key] = value
    return value


def _recent_unique_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in reversed(rows):
        row_id = str(row.get("id") or "")
        if not row_id or row_id in seen:
            continue
        seen.add(row_id)
        unique.append(row)
    return list(reversed(unique))


def _evidence_source_urls(ledger: object) -> set[str]:
    urls: set[str] = set()
    for item in getattr(ledger, "evidence_items", ()):
        raw = str(item.source_url or "").strip()
        if raw:
            urls.add(ledger.canonical_opened_url(raw) or raw)
    return urls


def _normalized_id(value: object) -> str:
    return str(value or "").strip().lower()


def _looks_like_source_id(value: str) -> bool:
    return len(value) > 1 and value[0] == "s" and value[1:].isdigit()


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
