"""Thin state-aware gate for Codey Research.

The controller is deliberately not a planner. It reads the per-run ledger,
exposes only currently reasonable tools, and rewrites stable local IDs into the
ordinary JSON tool arguments that the existing codec already validates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from codey.models import ToolPlan
from codey.research.protocols import ProtocolCodec, extract_json_objects
from codey.research.source_document import compact_pages
from codey.research.tool_contract import (
    PROTOCOL_DISALLOWED_TOOL,
    PROTOCOL_INVALID_ARGS,
    tool_example,
)

CONTROLLER_DISPLAY_LIMIT = 8


def controller_system_prompt(*, include_source_search: bool = True) -> str:
    tool_names = "web_search/open_url/knowledge_search/knowledge_read/knowledge_write/knowledge_link"
    if include_source_search:
        tool_names += "/source_search"
    source_search_line = (
        "\n- source_search is a locator inside an already-opened source, not evidence. "
        "Open the returned hit_id/offset/pages before citing."
        if include_source_search
        else ""
    )
    return f"""Research hard boundary:
- Reply only with one JSON tool call. Do not write the research answer directly.
- Choose exactly one tool. If you need another action, wait for the next local tool result first.
- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.
- Use only these local JSON tools for web and knowledge access: {tool_names}.
- Tool outputs are the only evidence.

You are a local research agent. You investigate a question using only Codey's
local JSON tools, then save what you learn into a local Markdown knowledge
library so it can be reused and audited later. You never invent facts.

Codey will append a "Research controller current allowed actions" block every
turn. Use only the tools and exact JSON shapes shown in that block.

Research discipline:
- A web_search result is not evidence. Open useful results before knowledge_write.
- Prefer result_id/source_id/hit_id over hand-copying URLs when an ID is available.{source_search_line}
- Evidence snippets must be exact short excerpts copied from open_url text.
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


class ResearchController:
    def __init__(self, *, include_source_search: bool = True) -> None:
        self.include_source_search = bool(include_source_search)
        self._result_ids_by_url: dict[str, str] = {}
        self._source_ids_by_url: dict[str, str] = {}
        self._source_urls_by_id: dict[str, str] = {}
        self._hit_ids_by_key: dict[str, str] = {}

    def build_state(self, tools: object, *, turn: int, max_turns: int) -> ResearchControlState:
        ledger = tools.ledger
        result_rows = self._result_rows(ledger)
        source_rows = self._source_rows(ledger)
        hit_rows = self._hit_rows(ledger)
        evidence_urls = _evidence_source_urls(ledger)
        evidence_count = len(ledger.evidence_items)
        note_count = len(getattr(tools, "created_ids", ())) + len(getattr(tools, "updated_ids", ()))
        grounded_count = len(getattr(tools, "grounded_ids", ()))
        activity = bool(ledger.searches or ledger.opened_sources)
        done_escape = evidence_count == 0 and activity and int(turn or 0) >= max(1, int(max_turns or 1) - 1)

        allowed = ["knowledge_search", "knowledge_read", "web_search"]
        if result_rows or source_rows or hit_rows:
            allowed.append("open_url")
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
        return ResearchControlState(
            allowed_tools=tuple(dict.fromkeys(allowed)),
            result_urls={row["id"]: row["url"] for row in result_rows},
            source_urls=dict(self._source_urls_by_id),
            hit_targets={row["id"]: row["target"] for row in hit_rows},
            result_lines=tuple(row["line"] for row in result_rows[-CONTROLLER_DISPLAY_LIMIT:]),
            source_lines=tuple(row["line"] for row in source_rows[-CONTROLLER_DISPLAY_LIMIT:]),
            hit_lines=tuple(row["line"] for row in hit_rows[-CONTROLLER_DISPLAY_LIMIT:]),
            citable_source_lines=citable[-CONTROLLER_DISPLAY_LIMIT:],
            noncitable_source_lines=noncitable[-CONTROLLER_DISPLAY_LIMIT:],
            evidence_count=evidence_count,
            note_count=note_count,
            done_escape=done_escape,
        )

    def parse_plan(self, codec: ProtocolCodec, reply: str, state: ResearchControlState) -> ToolPlan:
        objects = extract_json_objects(reply or "")
        if len(objects) == 1:
            rewritten, error = rewrite_id_args(objects[0], state)
            if error:
                return ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error=error,
                    protocol_error_kind=PROTOCOL_INVALID_ARGS,
                )
            plan = codec.parse(json.dumps(rewritten, ensure_ascii=False))
        else:
            plan = codec.parse(reply)
        if plan.protocol_error or (not plan.calls and plan.control is None):
            return plan
        tool = plan.control.kind if plan.control is not None else plan.calls[0].name
        if state.allowed_tools and tool not in state.allowed_tools:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=(
                    f"{tool} is not allowed by the current Research controller state; "
                    f"allowed tools: {', '.join(state.allowed_tools)}"
                ),
                protocol_error_kind=PROTOCOL_DISALLOWED_TOOL,
            )
        return plan

    def append_block(self, message: str, state: ResearchControlState) -> str:
        return str(message or "").rstrip() + "\n\n" + render_control_block(state)

    def _result_rows(self, ledger: object) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for search in getattr(ledger, "searches", ()):
            for result in search.results:
                url = str(result.url or "").strip()
                if not url:
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


def rewrite_id_args(obj: dict[str, Any], state: ResearchControlState) -> tuple[dict[str, Any], str]:
    rewritten = dict(obj)
    args = rewritten.get("args")
    args = dict(args) if isinstance(args, dict) else {
        key: value for key, value in rewritten.items() if key not in ("tool", "name")
    }
    tool = str(rewritten.get("tool") or rewritten.get("name") or "").strip().lower()
    if tool in {"open_url", "open", "fetch", "read_url"}:
        error = _rewrite_open_url_args(args, state)
        if error:
            return rewritten, error
    elif tool in {"source_search", "search_source", "find_in_source"}:
        error = _rewrite_source_search_args(args, state)
        if error:
            return rewritten, error
    elif tool in {"knowledge_write", "note_write", "save_note", "write_note"}:
        error = _rewrite_knowledge_write_args(args, state)
        if error:
            return rewritten, error
    rewritten["args"] = args
    return rewritten, ""


def render_control_block(state: ResearchControlState) -> str:
    lines = [
        "Research controller current allowed actions:",
        f"- Allowed tools this turn: {', '.join(state.allowed_tools)}",
        "- Reply with exactly one JSON object using only the allowed tools below.",
        "- Prefer result_id/source_id/hit_id over hand-copying URLs when an ID is available.",
        f"- Saved evidence items: {state.evidence_count}; saved/updated notes: {state.note_count}.",
        "- In done.answer, cite and list only evidence-backed source URLs. Opened-only sources are not citable yet.",
    ]
    if state.done_escape and state.evidence_count == 0:
        lines.append(
            "- done is allowed only to report insufficient/no citable evidence and what was searched."
        )
    lines.extend(("", "Allowed JSON shapes:"))
    for tool in state.allowed_tools:
        for example in _tool_examples(tool, state):
            lines.append(f"- {example}")
    if state.result_lines:
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


def _rewrite_open_url_args(args: dict[str, Any], state: ResearchControlState) -> str:
    hit_id = _normalized_id(args.get("hit_id"))
    result_id = _normalized_id(args.get("result_id"))
    source_id = _normalized_id(args.get("source_id"))
    if hit_id:
        target = state.hit_targets.get(hit_id)
        if target is None:
            return f"unknown hit_id: {hit_id}"
        args["url"] = target.url
        args["offset"] = target.offset
        args["limit"] = target.limit
        args["pages"] = target.pages
    elif result_id:
        url = state.result_urls.get(result_id, "")
        if not url:
            return f"unknown result_id: {result_id}"
        args["url"] = url
    elif source_id:
        url = state.source_urls.get(source_id, "")
        if not url:
            return f"unknown source_id: {source_id}"
        args["url"] = url
    return ""


def _rewrite_source_search_args(args: dict[str, Any], state: ResearchControlState) -> str:
    source_id = _normalized_id(args.get("source_id"))
    if not source_id:
        return ""
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
    if tool == "open_url":
        examples: list[str] = []
        if state.hit_targets:
            hid = next(iter(state.hit_targets))
            examples.append(f'{{"tool":"open_url","args":{{"hit_id":"{hid}"}}}}')
        if state.result_urls:
            rid = next(iter(state.result_urls))
            examples.append(f'{{"tool":"open_url","args":{{"result_id":"{rid}"}}}}')
        if state.source_urls:
            sid = next(iter(state.source_urls))
            examples.append(
                f'{{"tool":"open_url","args":{{"source_id":"{sid}","offset":0,"limit":6000,"pages":""}}}}'
            )
        return tuple(examples) or (tool_example("open_url"),)
    if tool == "source_search":
        if state.source_urls:
            sid = next(iter(state.source_urls))
            return (f'{{"tool":"source_search","args":{{"source_id":"{sid}","query":"...","limit":6}}}}',)
        return (tool_example("source_search"),)
    if tool == "knowledge_write" and state.source_urls:
        sid = next(iter(state.source_urls))
        return (
            '{"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...",'
            f'"sources":["{sid}"],"evidence":{{"claim":"...","source_url":"{sid}",'
            '"excerpt":"exact short text from open_url","stance":"supports"}}}}',
        )
    return (tool_example(tool),)


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
