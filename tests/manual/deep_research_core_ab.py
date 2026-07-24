"""Live A/B probe for Deep Research Core ideas.

This script is intentionally outside normal pytest collection. It does not
change production Research behavior. The live provider still performs a real
JSON-tool research loop, but web search, source bodies, PDFs, and seeded local
notes are deterministic fixtures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ruff: noqa: E402 - direct script execution must add the repository root first.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey import cancellation
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.models import Control, ToolCall, ToolPlan
from codey.providers.registry import connect_provider, provider_ids
from codey.research.pdf_extract import PDF_DEFAULT_PAGES, parse_pages
from codey.research.protocols import JsonToolCodec, MAX_CALLS_PER_TURN
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunner, _Outcome
from codey.research.source_document import SourceDocument, SourcePage
from codey.research.tools import ResearchTools


ARMS = ("baseline", "source_search", "deep_core")
PROFILES = ("cheap", "full")
DEFAULT_PROFILE = "cheap"
CHEAP_CASE_NAMES = ("long-official-doc", "pdf-target-page")
CHEAP_ARMS = ARMS
CHEAP_MAX_TURNS = 10
FULL_MAX_TURNS = 14
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-deep-research-core-ab.json"
SOURCE_SEARCH_SNIPPET_CHARS = 460
MAX_REPLY_CHARS = 4_000
MAX_FIELD_CHARS = 2_000
MAX_RAW_REPLY_PREVIEW_CHARS = 1_000


@dataclass(frozen=True)
class FixtureDocument:
    url: str
    title: str
    text: str = ""
    pages: tuple[str, ...] = ()
    snippet: str = ""

    @property
    def content_kind(self) -> str:
        return "pdf" if self.pages else "html"

    @property
    def full_text(self) -> str:
        if not self.pages:
            return self.text
        return "\n\n".join(
            f"[page {index}]\n{page}"
            for index, page in enumerate(self.pages, 1)
        )

    def result(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet or _clip(self.full_text, 220),
        }

    def source_document(self, *, pages: str = "") -> SourceDocument:
        if not self.pages:
            return SourceDocument.html(
                requested_url=self.url,
                final_url=self.url,
                title=self.title,
                text=self.text,
            )
        selected = parse_pages(
            pages,
            default=pages or PDF_DEFAULT_PAGES,
        )
        page_texts: list[SourcePage] = []
        parts: list[str] = []
        total = 0
        for number in selected:
            if number < 1 or number > len(self.pages):
                continue
            body = self.pages[number - 1].strip()
            if not body:
                continue
            marker = f"[page {number}]\n"
            char_start = total + len(marker)
            char_end = char_start + len(body)
            parts.append(marker + body)
            page_texts.append(SourcePage(number, body, char_start, char_end))
            total += len(marker) + len(body) + 2
        return SourceDocument(
            requested_url=self.url,
            final_url=self.url,
            title=self.title,
            content_kind="pdf",
            mime_type="application/pdf",
            text="\n\n".join(parts).strip(),
            page_count=len(self.pages),
            pages_read=tuple(page.number for page in page_texts),
            page_texts=tuple(page_texts),
            truncated=bool(set(selected) - {page.number for page in page_texts}),
        )


@dataclass(frozen=True)
class SeedNote:
    type: str
    title: str
    body: str
    sources: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchProbeCase:
    name: str
    question: str
    documents: tuple[FixtureDocument, ...]
    expected_primary_urls: tuple[str, ...] = ()
    expected_target_url: str = ""
    expected_report_terms: tuple[str, ...] = ()
    expected_evidence_terms: tuple[str, ...] = ()
    expected_target_page: int = 0
    expected_target_offset: int = 0
    counter_urls: tuple[str, ...] = ()
    counter_terms: tuple[str, ...] = ()
    seed_notes: tuple[SeedNote, ...] = ()
    local_terms: tuple[str, ...] = ()


OFFICIAL_LONG_URL = "https://agency.gov/alpha-safety/manual"
PDF_METHOD_URL = "https://example.edu/omega-method.pdf"
MEDIA_URL = "https://markets.example/news/examplecorp-capex"
IR_URL = "https://investors.examplecorp.com/2026-capex-guidance"
SUPPORT_URL = "https://docs.example.org/lithium-storage"
LIMIT_URL = "https://lab.example.org/lithium-storage-limitations"
MEMORY_URL = "https://standards.example.org/widget-storage"


def _long_official_text() -> str:
    intro = (
        "Alpha Safety Program official manual. "
        "This public document defines reporting roles, definitions, forms, "
        "and examples. "
    )
    filler = " ".join(f"background-section-{index}" for index in range(850))
    target = (
        "The required threshold is a 72-hour incident notification after a "
        "reportable safety breach is confirmed. The limit does not apply to "
        "near-miss observations that remain below the reportable breach level."
    )
    return f"{intro}\n\n{filler}\n\n{target}\n"


def _pdf_pages() -> tuple[str, ...]:
    return tuple(
        (
            "Omega paper front matter and related work."
            if page != 9
            else (
                "Methods. The validation method uses stratified bootstrap "
                "validation with 5,000 resamples and reports uncertainty as "
                "the median absolute error interval."
            )
        )
        for page in range(1, 11)
    )


CASES = (
    ResearchProbeCase(
        name="long-official-doc",
        question=(
            "Research whether the Alpha Safety Program requires a 72-hour "
            "incident notification threshold, and explain any limit."
        ),
        documents=(
            FixtureDocument(
                OFFICIAL_LONG_URL,
                "Alpha Safety Program official manual",
                text=_long_official_text(),
                snippet="Official Alpha Safety manual. Long document; details inside.",
            ),
            FixtureDocument(
                "https://blog.example/alpha-safety-summary",
                "Alpha Safety summary blog",
                text="A short secondary summary says the program has reporting rules.",
                snippet="Secondary summary of Alpha Safety reporting rules.",
            ),
        ),
        expected_primary_urls=(OFFICIAL_LONG_URL,),
        expected_target_url=OFFICIAL_LONG_URL,
        expected_report_terms=("72-hour incident notification", "near-miss"),
        expected_evidence_terms=("72-hour incident notification", "near-miss"),
        expected_target_offset=18_500,
    ),
    ResearchProbeCase(
        name="pdf-target-page",
        question=(
            "Research what validation method the Omega paper uses and cite the "
            "specific PDF page where the method appears."
        ),
        documents=(
            FixtureDocument(
                PDF_METHOD_URL,
                "Omega method paper",
                pages=_pdf_pages(),
                snippet="PDF paper with methods section later in the document.",
            ),
        ),
        expected_primary_urls=(PDF_METHOD_URL,),
        expected_target_url=PDF_METHOD_URL,
        expected_report_terms=("stratified bootstrap validation",),
        expected_evidence_terms=("stratified bootstrap validation", "5,000 resamples"),
        expected_target_page=9,
    ),
    ResearchProbeCase(
        name="primary-vs-media",
        question=(
            "Research whether ExampleCorp changed its 2026 capex guidance. "
            "Prefer a primary source over media summaries."
        ),
        documents=(
            FixtureDocument(
                MEDIA_URL,
                "ExampleCorp capex rises, analysts say",
                text=(
                    "A market story says analysts believe ExampleCorp raised "
                    "2026 capex guidance to $4.2 billion."
                ),
                snippet="Analysts cite a possible capex guidance change.",
            ),
            FixtureDocument(
                IR_URL,
                "ExampleCorp investor release on 2026 capex",
                text=(
                    "Company investor release. ExampleCorp updated 2026 capital "
                    "expenditure guidance from $3.6 billion to $4.2 billion. "
                    "Management says the change funds capacity expansion."
                ),
                snippet="Company investor release with updated guidance.",
            ),
        ),
        expected_primary_urls=(IR_URL,),
        expected_target_url=IR_URL,
        expected_report_terms=("from $3.6 billion to $4.2 billion",),
        expected_evidence_terms=("from $3.6 billion to $4.2 billion",),
    ),
    ResearchProbeCase(
        name="counter-limits",
        question=(
            "Research whether lithium storage retrofits are practical for small "
            "warehouses, including counter-evidence or limitations."
        ),
        documents=(
            FixtureDocument(
                SUPPORT_URL,
                "Lithium retrofit guide",
                text=(
                    "The guide says lithium storage retrofits can reduce peak "
                    "demand charges in small warehouses with predictable loads."
                ),
                snippet="Guide describing benefits of lithium retrofits.",
            ),
            FixtureDocument(
                LIMIT_URL,
                "Lithium retrofit limitations note",
                text=(
                    "Limitation: retrofits are often impractical where fire-code "
                    "setbacks require a separated battery room and ventilation "
                    "retrofit costs exceed demand-charge savings."
                ),
                snippet="Limitations for small warehouse lithium retrofits.",
            ),
        ),
        expected_primary_urls=(SUPPORT_URL,),
        expected_target_url=SUPPORT_URL,
        expected_report_terms=("reduce peak demand charges",),
        expected_evidence_terms=("reduce peak demand charges",),
        counter_urls=(LIMIT_URL,),
        counter_terms=("fire-code setbacks", "ventilation retrofit"),
    ),
    ResearchProbeCase(
        name="local-memory",
        question=(
            "Research the current Widget Storage API decision and say whether "
            "the prior local note should still be reused."
        ),
        documents=(
            FixtureDocument(
                MEMORY_URL,
                "Widget Storage standard",
                text=(
                    "The Widget Storage standard still recommends the "
                    "stable-v2 endpoint for client storage integration."
                ),
                snippet="Stable-v2 endpoint remains recommended.",
            ),
        ),
        expected_primary_urls=(MEMORY_URL,),
        expected_target_url=MEMORY_URL,
        expected_report_terms=("stable-v2 endpoint",),
        expected_evidence_terms=("stable-v2 endpoint",),
        seed_notes=(
            SeedNote(
                "fact",
                "Prior Widget Storage API decision",
                "Prior local memory says Widget Storage should use stable-v2.",
                sources=(MEMORY_URL,),
                tags=("research", "widget"),
            ),
        ),
        local_terms=("Prior Widget Storage API decision", "stable-v2"),
    ),
)


class FixtureSearchProvider:
    name = "fixture-search"

    def __init__(self, case: ResearchProbeCase) -> None:
        self.case = case
        self.queries: list[str] = []
        self.fetches: list[str] = []

    def search(self, query: str, limit: int = 8) -> list[dict]:
        self.queries.append(str(query or ""))
        return [doc.result() for doc in self.case.documents[:limit]]

    def fetch(self, url: str) -> dict:
        self.fetches.append(str(url or ""))
        doc = self.document_for_url(url)
        if doc is None:
            return {
                "url": url,
                "title": "",
                "text": "ERROR: fixture URL not found",
                "truncated": False,
            }
        return {
            "url": doc.url,
            "title": doc.title,
            "text": "",
            "source_document": doc,
            "content_kind": doc.content_kind,
            "mime_type": "application/pdf" if doc.pages else "text/html",
            "truncated": False,
        }

    def document_for_url(self, url: str) -> FixtureDocument | None:
        normalized = str(url or "").strip()
        for doc in self.case.documents:
            if normalized == doc.url:
                return doc
        return None

    def close(self) -> None:
        pass


class ProbeResearchTools(ResearchTools):
    def _source_document_from_fetch(
        self,
        requested_url: str,
        page: dict,
        *,
        pages: str = "",
    ) -> SourceDocument:
        doc = page.get("source_document")
        if isinstance(doc, FixtureDocument):
            return doc.source_document(pages=pages)
        return super()._source_document_from_fetch(
            requested_url,
            page,
            pages=pages,
        )

    def source_search(
        self,
        url: str,
        query: str,
        limit: int = 6,
    ) -> str:
        url = str(url or "").strip()
        query = str(query or "").strip()
        if not url:
            return "ERROR: source_search needs a url"
        if not query:
            return "ERROR: source_search needs a query"
        final_url = self.ledger.canonical_opened_url(url)
        if not final_url:
            return "NEEDS_OPEN: open_url before source_search: " + url
        search_doc = getattr(self.search, "document_for_url", lambda _url: None)
        doc = search_doc(final_url) or search_doc(url)
        if doc is None:
            return "ERROR: source_search fixture document is unavailable"
        hits = _source_hits(doc, query, max(1, min(12, _as_int(limit, 6))))
        if not hits:
            return "no source_search matches"
        lines = [
            "source_search results from an already-opened source.",
            "Use these as locators. Open the matching offset/pages before relying on them as evidence.",
        ]
        for index, hit in enumerate(hits, 1):
            locator = f"p.{hit['page']}" if hit.get("page") else f"offset {hit['offset']}"
            lines.append(f"{index}. {locator}: {hit['snippet']}")
        return "\n".join(lines)


class ProbeResearchRunner(ResearchRunner):
    def __init__(
        self,
        provider,
        search,
        store: KnowledgeStore,
        *,
        arm: str,
        max_turns: int,
        send_timeout: float = 120.0,
        provider_id: str = "",
        case_name: str = "",
        trace: LiveTrace | None = None,
    ) -> None:
        super().__init__(
            provider,
            search,
            store,
            max_turns=max_turns,
            codec=ProbeJsonToolCodec(arm),
            session_id=f"deep-research-ab-{arm}",
        )
        self.arm = arm
        self.send_timeout = max(1.0, float(send_timeout))
        self.provider_id = provider_id
        self.case_name = case_name
        self.trace = trace
        self.sent_messages: list[str] = []
        self.received_replies: list[str] = []
        self.tools = ProbeResearchTools(
            search=search,
            store=store,
            changes=self.changes,
            session_id=f"deep-research-ab-{arm}",
        )

    def _send_provider(self, message: str) -> str:
        self.sent_messages.append(message)
        try:
            cancellation.check()
            reply = self.provider.send(message, timeout=self.send_timeout)
            cancellation.check()
            text = str(reply or "")
            self.received_replies.append(text)
            if self.trace is not None:
                self.trace.record_reply(
                    provider=self.provider_id,
                    case=self.case_name,
                    arm=self.arm,
                    send_index=len(self.sent_messages),
                    message=message,
                    reply=text,
                )
            return text
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            if self.trace is not None:
                self.trace.record({
                    "event": "send_error",
                    "provider": self.provider_id,
                    "case": self.case_name,
                    "arm": self.arm,
                    "send_index": len(self.sent_messages),
                    "error": f"{type(exc).__name__}: {exc}",
                    "message_preview": _clip(message, 800),
                })
            self._record_model_failure("send", exc)
            raise

    def _dispatch(self, call):
        if call.name == "source_search":
            return _Outcome(self.tools.source_search(
                str(call.args.get("url") or ""),
                str(call.args.get("query") or ""),
                _as_int(call.args.get("limit"), 6),
            ))
        return super()._dispatch(call)


class TimedProvider:
    def __init__(
        self,
        provider,
        *,
        send_timeout: float,
        new_chat_timeout: float,
        no_new_chat: bool = False,
    ) -> None:
        self.provider = provider
        self.send_timeout = max(1.0, float(send_timeout))
        self.new_chat_timeout = max(1.0, float(new_chat_timeout))
        self.no_new_chat = no_new_chat
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")

    def new_chat(self, timeout=None) -> None:
        if self.no_new_chat:
            return None
        effective = self.new_chat_timeout if timeout is None else timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout=None) -> str:
        effective = self.send_timeout if timeout is None else timeout
        return self.provider.send(text, timeout=effective)

    def close(self) -> None:
        return self.provider.close()


class LiveTrace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.events.append(payload)
        self.flush()

    def record_reply(
        self,
        *,
        provider: str,
        case: str,
        arm: str,
        send_index: int,
        message: str,
        reply: str,
    ) -> None:
        self.record({
            "event": "reply",
            "provider": provider,
            "case": case,
            "arm": arm,
            "send_index": send_index,
            "sent_chars": len(message or ""),
            "reply_chars": len(reply or ""),
            "message_preview": _clip(message, 800),
            "reply": _clip(reply, MAX_REPLY_CHARS),
        })

    def flush(self) -> None:
        _write_json_atomic(
            self.path,
            {
                "probe": "deep_research_core_ab_trace",
                "updated_elapsed_seconds": round(time.monotonic() - self.started, 3),
                "event_count": len(self.events),
                "events": self.events,
            },
        )


@dataclass(frozen=True)
class ProbeJsonToolCodec(JsonToolCodec):
    arm: str = "baseline"

    def system_prompt(self) -> str:
        include_source_search = self.arm in {"source_search", "deep_core"}
        base = _with_probe_fixture_discipline(
            super().system_prompt(),
            include_source_search=include_source_search,
        )
        if self.arm == "baseline":
            return base
        prompt = _with_source_search_tool(base)
        if self.arm == "deep_core":
            prompt += "\n\n" + _DEEP_CORE_APPENDIX
        return prompt

    def repair_prompt(self) -> str:
        if self.arm == "baseline":
            return super().repair_prompt() + "\n\n" + _probe_fixture_reminder(include_source_search=False)
        return (
            "Your last reply was not a valid tool call. Reply with exactly one "
            "JSON object and nothing else, for example:\n"
            '{"tool":"web_search","args":{"query":"..."}}\n'
            '{"tool":"source_search","args":{"url":"https://...","query":"..."}}\n'
            'or {"tool":"done","args":{"answer":"..."}}'
            "\n\n"
            + _probe_fixture_reminder(include_source_search=True)
        )

    def format_results(self, results) -> str:
        return (
            super().format_results(results)
            + "\n\n"
            + _probe_fixture_reminder(include_source_search=self.arm in {"source_search", "deep_core"})
        )

    def parse(self, text: str) -> ToolPlan:
        objects = _extract_json_objects(text or "")
        if not objects:
            answer = _extract_malformed_done_answer(text or "")
            if answer:
                return ToolPlan(calls=[], control=Control("done", answer))
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
                args = {key: value for key, value in obj.items() if key not in ("tool", "name")}
            if runtime == "done":
                body = str(args.get("answer") or args.get("summary") or args.get("text") or "")
                control = Control("done", body)
            else:
                calls.append(ToolCall(runtime, args))
        if not calls and control is None:
            return ToolPlan(calls=[], control=None, protocol_error="no known tool in reply")
        action_count = len(calls) + (1 if control is not None else 0)
        if action_count > 1:
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=(
                    f"too many JSON tool calls in one reply ({action_count}); "
                    "reply with exactly one JSON object"
                ),
            )
        return ToolPlan(calls=calls[:MAX_CALLS_PER_TURN], control=control)


_ALIAS_TO_TOOL = {
    "web_search": "web_search",
    "search": "web_search",
    "open_url": "open_url",
    "open": "open_url",
    "fetch": "open_url",
    "read_url": "open_url",
    "source_search": "source_search",
    "search_source": "source_search",
    "find_in_source": "source_search",
    "knowledge_search": "knowledge_search",
    "recall": "knowledge_search",
    "memory_search": "knowledge_search",
    "vault_search": "knowledge_search",
    "knowledge_read": "knowledge_read",
    "knowledge_note": "knowledge_read",
    "vault_read": "knowledge_read",
    "note_read": "knowledge_read",
    "knowledge_write": "knowledge_write",
    "note_write": "knowledge_write",
    "save_note": "knowledge_write",
    "write_note": "knowledge_write",
    "knowledge_link": "knowledge_link",
    "note_link": "knowledge_link",
    "link": "knowledge_link",
    "done": "done",
    "answer": "done",
    "finish": "done",
}

_DEEP_CORE_APPENDIX = """Deep Research Core experimental guidance:
- Follow a compact coverage plan before done: local memory, overview, primary/source-of-record, data or method, counter-evidence or limitations, freshness if time-sensitive, and application implications if the user may later build from this.
- Prefer primary or source-of-record material when available: official docs, company investor relations, filings, standards, datasets, papers, or original methods. Use media/blog/forum sources mainly as leads or context.
- Use source_search after opening a long page or PDF when the needed fact may be outside the first visible window. For PDFs, use source_search to locate pages, then open_url with pages="N" before citing page-specific evidence.
- Coverage is not proof. If a coverage leg is missing, say what was searched and what would change the conclusion instead of pretending the search was complete.
- Keep the final report focused. Do not add template filler; every section should contain concrete evidence, limitation, source quality, or coverage facts."""


def _with_source_search_tool(prompt: str) -> str:
    needle = (
        '- {"tool":"open_url","args":{"url":"https://...",'
        '"offset":0,"limit":6000,"pages":"1-5"}}  read a page\'s text; '
        "for PDFs, pages selects bounded page ranges\n"
    )
    insert = (
        needle
        + '- {"tool":"source_search","args":{"url":"https://...",'
        '"query":"...","limit":6}}  search within a source already opened with '
        "open_url; returns locators, offsets, and PDF pages\n"
    )
    if needle in prompt:
        prompt = prompt.replace(needle, insert)
    else:
        prompt += (
            "\n- source_search(url, query, limit) searches inside an already-opened source "
            "and returns locators only.\n"
        )
    return prompt + (
        "\nsource_search discipline: call open_url first; source_search is a locator, "
        "not independent evidence. Open the returned offset or PDF page before citing "
        "that detail when page/offset context matters."
    )


def _with_probe_fixture_discipline(prompt: str, *, include_source_search: bool) -> str:
    return prompt + "\n\n" + _probe_fixture_reminder(include_source_search=include_source_search)


def _probe_fixture_reminder(*, include_source_search: bool) -> str:
    tool_names = "web_search/open_url/knowledge_search/knowledge_read/knowledge_write/knowledge_link"
    if include_source_search:
        tool_names += "/source_search"
    return (
        "Probe fixture discipline:\n"
        "- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.\n"
        f"- All web and knowledge access in this probe is available only through these JSON tools: {tool_names}.\n"
        "- The source URLs and facts are deterministic local fixtures; they may not exist on the public internet.\n"
        "- Treat tool outputs as the only evidence, even when a fixture domain looks fake or unreachable publicly.\n"
        "- Do not answer from memory or from the chat website's own search results."
    )


def _source_hits(
    doc: FixtureDocument,
    query: str,
    limit: int,
) -> list[dict[str, object]]:
    tokens = _tokens(query)
    hits: list[dict[str, object]] = []
    if doc.pages:
        for page_number, text in enumerate(doc.pages, 1):
            hit = _best_hit(text, tokens)
            if hit is None:
                continue
            hits.append({
                "page": page_number,
                "offset": hit,
                "snippet": _snippet(text, hit),
            })
    else:
        text = doc.text
        offset = 0
        while offset < len(text):
            chunk = text[offset : offset + 2_000]
            hit = _best_hit(chunk, tokens)
            if hit is not None:
                absolute = offset + hit
                hits.append({
                    "offset": absolute,
                    "snippet": _snippet(text, absolute),
                })
            offset += 1_600
    return hits[:limit]


def _best_hit(text: str, tokens: tuple[str, ...]) -> int | None:
    lowered = text.lower()
    positions = [lowered.find(token) for token in tokens if token]
    found = [pos for pos in positions if pos >= 0]
    if not found:
        return None
    return min(found)


def _tokens(query: str) -> tuple[str, ...]:
    raw = re.findall(r"[A-Za-z0-9$.-]{3,}", str(query or "").lower())
    tokens: list[str] = []
    for item in raw:
        cleaned = item.strip(".-")
        if cleaned and cleaned not in tokens:
            tokens.append(cleaned)
    return tuple(tokens[:8])


def _snippet(text: str, index: int) -> str:
    start = max(0, index - 130)
    return _clip(" ".join(text[start : start + SOURCE_SEARCH_SNIPPET_CHARS].split()), SOURCE_SEARCH_SNIPPET_CHARS)


def _seed_store(store: KnowledgeStore, case: ResearchProbeCase) -> None:
    for seed in case.seed_notes:
        note = KnowledgeNote.create(
            type=seed.type,
            title=seed.title,
            body=seed.body,
            sources=list(seed.sources),
            tags=list(seed.tags),
            session_id="deep-research-ab-baseline",
        )
        store.write_note(note)


def run_case(
    provider,
    provider_id: str,
    case: ResearchProbeCase,
    arm: str,
    max_turns: int,
    timeout: float,
    trace: LiveTrace | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codey-deep-research-ab-") as td:
        store = KnowledgeStore(Path(td))
        _seed_store(store, case)
        search = FixtureSearchProvider(case)
        runner = ProbeResearchRunner(
            provider,
            search,
            store,
            arm=arm,
            max_turns=max_turns,
            send_timeout=timeout,
            provider_id=provider_id,
            case_name=case.name,
            trace=trace,
        )
        events = []
        started = time.monotonic()
        try:
            for event in runner.run(case.question):
                events.append(event)
            result = runner.result
            if result is None:
                raise RuntimeError("research finished without result")
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "send_count": len(runner.sent_messages),
                "reply_count": len(runner.received_replies),
                "sent_chars": sum(len(item) for item in runner.sent_messages),
                "reply_chars": sum(len(item) for item in runner.received_replies),
                "first_prompt_chars": len(runner.sent_messages[0]) if runner.sent_messages else 0,
                "done_attempts": _done_attempt_count(runner.received_replies),
                "protocol_repair_prompts": _sent_prompt_count(
                    runner.sent_messages,
                    "Your last reply was not a valid tool call",
                ),
                "quality_repair_prompts": _sent_prompt_count(
                    runner.sent_messages,
                    "research quality review",
                ),
                "raw_reply_previews": [
                    _clip(reply, MAX_RAW_REPLY_PREVIEW_CHARS)
                    for reply in runner.received_replies[-3:]
                ],
                "stop_reason": result.stop_reason,
                "turns_used": result.turns,
                "queries": list(result.queries),
                "source_urls": list(result.source_urls),
                "opened_sources": list(result.opened_sources),
                "citation_map": result.citation_map,
                "evidence_items": result.evidence_items,
                "quality_warnings": result.quality_warnings,
                "summary_preview": _clip(result.summary, MAX_REPLY_CHARS),
                "tool_calls": _tool_calls(events),
            }
            row["last_done_quality_review"] = _last_done_quality_review(runner)
            row.update(_score(case, row, result))
            return row
        finally:
            store.close()


def _tool_calls(events: list[object]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        call = getattr(event, "call", None)
        if getattr(event, "kind", "") != "tool" or call is None:
            continue
        outcome = getattr(event, "outcome", None)
        rows.append({
            "name": call.name,
            "args": dict(call.args),
            "status": getattr(outcome, "status", ""),
            "result": outcome.first_line(240) if outcome is not None else "",
        })
    return rows


def _score(
    case: ResearchProbeCase,
    row: dict[str, Any],
    result,
) -> dict[str, Any]:
    tool_calls = row["tool_calls"]
    model_text = _model_claim_text(tool_calls, result.summary)
    summary_text = str(result.summary or "").lower()
    evidence_text = "\n".join(
        str(item.get("excerpt") or "")
        for item in result.evidence_items
    ).lower()
    opened_urls = set(result.source_urls)
    used_source_search = any(call["name"] == "source_search" for call in tool_calls)
    primary_applicable = bool(case.expected_primary_urls)
    target_report_applicable = bool(case.expected_report_terms)
    target_evidence_applicable = bool(case.expected_evidence_terms)
    target_locator_applicable = bool(case.expected_target_page or case.expected_target_offset)
    counter_applicable = bool(case.counter_terms)
    local_memory_applicable = bool(case.local_terms)
    opened_primary_source = (
        not case.expected_primary_urls
        or bool(opened_urls.intersection(case.expected_primary_urls))
    )
    saved_exact_evidence_snippet = (
        not case.expected_evidence_terms
        or all(term.lower() in evidence_text for term in case.expected_evidence_terms)
    )
    target_fact_reported = (
        not case.expected_report_terms
        or all(term.lower() in model_text for term in case.expected_report_terms)
    )
    opened_target_page_or_offset = _opened_target_locator(case, tool_calls, result)
    reported_counter_or_limits = (
        not case.counter_terms
        or (
            bool(opened_urls.intersection(case.counter_urls))
            and all(term.lower() in summary_text for term in case.counter_terms)
        )
    )
    used_local_memory = (
        not case.local_terms
        or (
            any(call["name"] == "knowledge_search" for call in tool_calls)
            and any(call["name"] == "knowledge_read" for call in tool_calls)
            and any(term.lower() in summary_text for term in case.local_terms)
        )
    )
    unsupported_citation_count = _unsupported_citation_count(result.summary, opened_urls)
    max_turns_failure = result.stop_reason == "max_turns"
    final_done = result.stop_reason == "done"
    quality_score = (
        (2 if final_done else 0)
        + (2 if opened_primary_source else 0)
        + (2 if target_fact_reported else 0)
        + (1 if saved_exact_evidence_snippet else 0)
        + (1 if opened_target_page_or_offset else 0)
        + (2 if reported_counter_or_limits else 0)
        + (1 if used_local_memory else 0)
        - min(3, unsupported_citation_count)
        - (2 if max_turns_failure else 0)
    )
    return {
        "final_done": final_done,
        "primary_applicable": primary_applicable,
        "opened_primary_source": opened_primary_source,
        "used_source_search": used_source_search,
        "target_locator_applicable": target_locator_applicable,
        "opened_target_page_or_offset": opened_target_page_or_offset,
        "target_report_applicable": target_report_applicable,
        "target_fact_reported": target_fact_reported,
        "target_evidence_applicable": target_evidence_applicable,
        "saved_exact_evidence_snippet": saved_exact_evidence_snippet,
        "counter_applicable": counter_applicable,
        "reported_counter_or_limits": reported_counter_or_limits,
        "local_memory_applicable": local_memory_applicable,
        "used_local_memory": used_local_memory,
        "unsupported_citation_count": unsupported_citation_count,
        "max_turns_failure": max_turns_failure,
        "quality_score": quality_score,
    }


def _model_claim_text(tool_calls: list[dict[str, Any]], summary: str) -> str:
    parts = [str(summary or "")]
    for call in tool_calls:
        if call.get("name") != "knowledge_write":
            continue
        args = call.get("args") or {}
        if not isinstance(args, dict):
            continue
        parts.extend((
            str(args.get("title") or ""),
            str(args.get("body") or ""),
        ))
        evidence = args.get("evidence")
        if isinstance(evidence, dict):
            evidence = [evidence]
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict):
                    parts.append(str(item.get("claim") or ""))
    return "\n".join(parts).lower()


def _opened_target_locator(
    case: ResearchProbeCase,
    tool_calls: list[dict[str, Any]],
    result,
) -> bool:
    if not case.expected_target_url:
        return True
    if case.expected_target_page:
        for source in result.opened_sources:
            if str(source.get("final_url") or "") != case.expected_target_url:
                continue
            pages = source.get("pages_read") or []
            if case.expected_target_page in [int(page) for page in pages]:
                return True
        return False
    if case.expected_target_offset:
        for call in tool_calls:
            if call["name"] != "open_url":
                continue
            args = call["args"]
            if str(args.get("url") or "") != case.expected_target_url:
                continue
            offset = _as_int(args.get("offset"), 0)
            if offset >= max(0, case.expected_target_offset - 1_000):
                return True
        return False
    return True


def _unsupported_citation_count(summary: str, opened_urls: set[str]) -> int:
    count = 0
    for url in re.findall(r"https?://[^\s<>)\]]+", str(summary or "")):
        clean = url.rstrip(".,;:)")
        if clean and clean not in opened_urls:
            count += 1
    return count


def run_provider(
    provider_id: str,
    *,
    port: int,
    open_if_missing: bool,
    no_new_chat: bool,
    arms: tuple[str, ...],
    cases: tuple[ResearchProbeCase, ...],
    max_turns: int,
    timeout: float,
    new_chat_timeout: float,
    trace: LiveTrace | None = None,
) -> dict[str, Any]:
    try:
        provider = connect_provider(
            provider_id,
            port=port,
            open_if_missing=open_if_missing,
            bring_to_front=open_if_missing,
        )
    except Exception as exc:
        rows = [
            {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "error": f"connect {type(exc).__name__}: {exc}",
            }
            for case in cases
            for arm in arms
        ]
        print(f"[{provider_id}] connect {type(exc).__name__}: {exc}")
        if trace is not None:
            trace.record({
                "event": "connect_error",
                "provider": provider_id,
                "error": f"connect {type(exc).__name__}: {exc}",
            })
        return {
            "provider": provider_id,
            "elapsed_seconds": 0.0,
            "rows": rows,
            "summary": _summarize(rows),
            "max_turns": max_turns,
            "timeout_seconds": timeout,
            "new_chat_timeout_seconds": new_chat_timeout,
            "open_if_missing": open_if_missing,
            "no_new_chat": no_new_chat,
            "error": rows[0]["error"] if rows else f"connect {type(exc).__name__}: {exc}",
        }
    provider = TimedProvider(
        provider,
        send_timeout=timeout,
        new_chat_timeout=new_chat_timeout,
        no_new_chat=no_new_chat,
    )
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        for case in cases:
            for arm in arms:
                if trace is not None:
                    trace.record({
                        "event": "case_start",
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                    })
                try:
                    row = run_case(
                        provider,
                        provider_id,
                        case,
                        arm,
                        max_turns,
                        timeout,
                        trace=trace,
                    )
                except Exception as exc:
                    row = {
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "error": f"{type(exc).__name__}: {exc}",
                        "provider_failure": _provider_failure_payload(exc, provider),
                    }
                rows.append(row)
                if trace is not None:
                    trace.record({
                        "event": "case_result",
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "row": row,
                    })
                if "error" in row:
                    print(f"[{provider_id} {case.name} {arm}] {row['error']}")
                else:
                    print(
                        f"[{provider_id} {case.name} {arm}] "
                        f"score={row['quality_score']} done={row['final_done']} "
                        f"primary={row['opened_primary_source']} "
                        f"fact={row['target_fact_reported']} "
                        f"evidence={row['saved_exact_evidence_snippet']} "
                        f"locator={row['opened_target_page_or_offset']} "
                        f"counter={row['reported_counter_or_limits']} "
                        f"memory={row['used_local_memory']} "
                        f"source_search={row['used_source_search']} "
                        f"turns={row['turns_used']} elapsed={row['elapsed_seconds']}s"
                    )
    finally:
        provider.close()
    return {
        "provider": provider_id,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rows": rows,
        "summary": _summarize(rows),
        "max_turns": max_turns,
        "timeout_seconds": timeout,
        "new_chat_timeout_seconds": new_chat_timeout,
        "open_if_missing": open_if_missing,
        "no_new_chat": no_new_chat,
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"arms": {}}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm and "error" not in row]
        if not arm_rows:
            summary["arms"][arm] = {"count": 0}
            continue
        summary["arms"][arm] = {
            "count": len(arm_rows),
            "avg_quality_score": _avg(arm_rows, "quality_score"),
            "final_done_rate": _rate(arm_rows, "final_done"),
            "opened_primary_source_rate": _conditional_rate(
                arm_rows,
                "opened_primary_source",
                "primary_applicable",
            ),
            "used_source_search_rate": _rate(arm_rows, "used_source_search"),
            "opened_target_page_or_offset_rate": _conditional_rate(
                arm_rows,
                "opened_target_page_or_offset",
                "target_locator_applicable",
            ),
            "target_fact_reported_rate": _conditional_rate(
                arm_rows,
                "target_fact_reported",
                "target_report_applicable",
            ),
            "saved_exact_evidence_snippet_rate": _conditional_rate(
                arm_rows,
                "saved_exact_evidence_snippet",
                "target_evidence_applicable",
            ),
            "reported_counter_or_limits_rate": _conditional_rate(
                arm_rows,
                "reported_counter_or_limits",
                "counter_applicable",
            ),
            "used_local_memory_rate": _conditional_rate(
                arm_rows,
                "used_local_memory",
                "local_memory_applicable",
            ),
            "max_turns_failure_rate": _rate(arm_rows, "max_turns_failure"),
            "unsupported_citation_count": sum(int(row.get("unsupported_citation_count") or 0) for row in arm_rows),
            "avg_turns_used": _avg(arm_rows, "turns_used"),
        }
    baseline = summary["arms"].get("baseline", {})
    for arm in ("source_search", "deep_core"):
        current = summary["arms"].get(arm, {})
        if not baseline.get("count") or not current.get("count"):
            continue
        summary[f"{arm}_delta_vs_baseline"] = {
            "avg_quality_score": round(
                float(current.get("avg_quality_score") or 0)
                - float(baseline.get("avg_quality_score") or 0),
                3,
            ),
            "saved_exact_evidence_snippet_rate": round(
                float(current.get("saved_exact_evidence_snippet_rate") or 0)
                - float(baseline.get("saved_exact_evidence_snippet_rate") or 0),
                3,
            ),
            "target_fact_reported_rate": round(
                float(current.get("target_fact_reported_rate") or 0)
                - float(baseline.get("target_fact_reported_rate") or 0),
                3,
            ),
            "opened_primary_source_rate": round(
                float(current.get("opened_primary_source_rate") or 0)
                - float(baseline.get("opened_primary_source_rate") or 0),
                3,
            ),
            "avg_turns_used": round(
                float(current.get("avg_turns_used") or 0)
                - float(baseline.get("avg_turns_used") or 0),
                3,
            ),
        }
    return summary


def _provider_failure_payload(exc: BaseException, provider: object | None = None) -> dict[str, str] | None:
    failure = getattr(exc, "failure", None)
    if failure is None and provider is not None:
        failure = getattr(provider, "last_failure", None)
    if failure is None and provider is not None:
        inner = getattr(provider, "provider", None)
        failure = getattr(inner, "last_failure", None)
    if failure is None or not hasattr(failure, "to_dict"):
        return None
    return failure.to_dict()


def _last_done_quality_review(runner: ProbeResearchRunner) -> dict[str, Any]:
    answer = _last_done_answer(runner.received_replies)
    if not answer:
        return {}
    try:
        review = review_report_quality(
            answer,
            ledger=runner.tools.ledger,
            opened_sources=runner.tools.sources_read,
            search_result_urls=runner.tools.search_result_urls,
        )
    except Exception as exc:
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": review.ok,
        "message": review.message,
        "warnings": list(review.warnings),
        "citation_map": review.citation_payload(),
    }


def _last_done_answer(replies: list[str]) -> str:
    for reply in reversed(replies):
        for obj in reversed(_extract_json_objects(reply or "")):
            name = str(obj.get("tool") or obj.get("name") or "").strip().lower()
            if name not in {"done", "answer", "finish"}:
                continue
            args = obj.get("args")
            if not isinstance(args, dict):
                args = obj
            answer = str(args.get("answer") or args.get("summary") or args.get("text") or "")
            if answer.strip():
                return answer
        answer = _extract_malformed_done_answer(reply or "")
        if answer:
            return answer
    return ""


def _done_attempt_count(replies: list[str]) -> int:
    count = 0
    for reply in replies:
        objects = _extract_json_objects(reply or "")
        done_objects = [
            obj for obj in objects
            if str(obj.get("tool") or obj.get("name") or "").strip().lower() in {"done", "answer", "finish"}
        ]
        if done_objects:
            count += len(done_objects)
        elif re.search(r'"(?:tool|name)"\s*:\s*"(?:done|answer|finish)"', reply or "", flags=re.I):
            count += 1
    return count


def _sent_prompt_count(messages: list[str], needle: str) -> int:
    folded = str(needle or "").lower()
    if not folded:
        return 0
    return sum(1 for message in messages if folded in str(message or "").lower())


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 3)


def _conditional_rate(
    rows: list[dict[str, Any]],
    key: str,
    applicable_key: str,
) -> float:
    scoped = [row for row in rows if row.get(applicable_key)]
    if not scoped:
        return 0.0
    return round(sum(1 for row in scoped if row.get(key)) / len(scoped), 3)


def _avg(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(float(row.get(key) or 0) for row in rows) / len(rows), 3)


def _selected_cases(
    names: list[str],
    max_cases: int = 0,
    *,
    profile: str = DEFAULT_PROFILE,
) -> tuple[ResearchProbeCase, ...]:
    cases = _profile_cases(profile)
    if names:
        wanted = set(names)
        cases = tuple(case for case in CASES if case.name in wanted)
        missing = wanted - {case.name for case in cases}
        if missing:
            raise SystemExit(f"unknown case(s): {', '.join(sorted(missing))}")
    if max_cases:
        cases = cases[:max_cases]
    return cases


def _selected_arms(values: list[str], *, profile: str = DEFAULT_PROFILE) -> tuple[str, ...]:
    if not values:
        return _profile_arms(profile)
    arms: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            item = item.strip()
            if item and item not in arms:
                arms.append(item)
    invalid = [arm for arm in arms if arm not in ARMS]
    if invalid:
        raise SystemExit(f"unknown arm(s): {', '.join(invalid)}")
    return tuple(arms)


def _profile_cases(profile: str) -> tuple[ResearchProbeCase, ...]:
    _ensure_profile(profile)
    if profile == "full":
        return CASES
    wanted = set(CHEAP_CASE_NAMES)
    return tuple(case for case in CASES if case.name in wanted)


def _profile_arms(profile: str) -> tuple[str, ...]:
    _ensure_profile(profile)
    if profile == "full":
        return ARMS
    return CHEAP_ARMS


def _profile_max_turns(profile: str, requested: int = 0) -> int:
    if requested:
        return max(1, requested)
    _ensure_profile(profile)
    if profile == "full":
        return FULL_MAX_TURNS
    return CHEAP_MAX_TURNS


def _ensure_profile(profile: str) -> None:
    if profile not in PROFILES:
        raise SystemExit(f"unknown profile: {profile}")


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(path, payload)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _trace_output_path(output: Path) -> Path:
    if output.suffix:
        return output.with_name(f"{output.stem}.trace.json")
    return output.with_name(f"{output.name}.trace.json")


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[truncated]"


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
                    try:
                        value = json.loads(text[start : index + 1], strict=False)
                    except json.JSONDecodeError:
                        value = None
                    if isinstance(value, dict):
                        objects.append(value)
                    start = -1
    return objects


def _extract_malformed_done_answer(text: str) -> str:
    value = str(text or "").strip()
    if not re.search(r'"(?:tool|name)"\s*:\s*"(?:done|answer|finish)"', value, flags=re.I):
        return ""
    match = re.search(r'"(?:answer|summary|text)"\s*:\s*"', value, flags=re.I)
    if not match:
        return ""
    end = value.rfind('"}}')
    if end < match.end():
        end = value.rfind('"}')
    if end < match.end():
        return ""
    answer = value[match.end():end]
    return (
        answer
        .replace(r"\\", "\\")
        .replace(r"\"", '"')
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\t", "\t")
        .strip()
    )


class ScriptedProvider:
    name = "scripted"
    location = "fixture://scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0

    def new_chat(self, timeout=None) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        if not self.replies:
            return json.dumps({"tool": "done", "args": {"answer": "done"}})
        return self.replies.pop(0)

    def close(self) -> None:
        pass


def self_test() -> int:
    case = next(item for item in CASES if item.name == "pdf-target-page")
    baseline_codec = ProbeJsonToolCodec("baseline")
    source_codec = ProbeJsonToolCodec("source_search")
    deep_codec = ProbeJsonToolCodec("deep_core")
    assert "source_search" not in JsonToolCodec().system_prompt()
    assert "Probe fixture discipline" in baseline_codec.system_prompt()
    assert "source_search" not in baseline_codec.system_prompt()
    assert "source_search" in source_codec.system_prompt()
    assert "Deep Research Core experimental guidance" not in source_codec.system_prompt()
    assert "Deep Research Core experimental guidance" in deep_codec.system_prompt()
    plan = source_codec.parse(
        '{"tool":"source_search","args":{"url":"https://example.edu/omega-method.pdf","query":"bootstrap"}}'
    )
    assert plan.calls and plan.calls[0].name == "source_search"

    with tempfile.TemporaryDirectory(prefix="codey-deep-research-self-") as td:
        store = KnowledgeStore(Path(td))
        search = FixtureSearchProvider(case)
        tools = ProbeResearchTools(search, store, KnowledgeChanges(store.root))
        before = tools.source_search(PDF_METHOD_URL, "bootstrap", 3)
        assert before.startswith("NEEDS_OPEN:")
        opened = tools.open_url(PDF_METHOD_URL)
        assert "[page 1]" in opened
        located = tools.source_search(PDF_METHOD_URL, "stratified bootstrap", 3)
        assert "p.9" in located
        assert "stratified bootstrap validation" in located
        page = tools.open_url(PDF_METHOD_URL, pages="9")
        assert "[page 9]" in page
        store.close()

    report = (
        "## Conclusion\n"
        "- The Omega method uses stratified bootstrap validation [1 p.9].\n\n"
        "## Key evidence\n"
        "- [1 p.9] The method uses stratified bootstrap validation.\n\n"
        "## Counter-evidence\n"
        "- No strong counter-evidence was found; this fixture only checks source_search.\n\n"
        "## Source quality\n"
        "- [1] primary paper source.\n\n"
        "## Search coverage\n"
        "- query: omega method validation\n\n"
        "## Sources\n"
        f"[1] Omega method paper - {PDF_METHOD_URL}"
    )
    provider = ScriptedProvider(
        json.dumps({"tool": "open_url", "args": {"url": PDF_METHOD_URL}}),
        json.dumps({
            "tool": "source_search",
            "args": {"url": PDF_METHOD_URL, "query": "stratified bootstrap"},
        }),
        json.dumps({"tool": "open_url", "args": {"url": PDF_METHOD_URL, "pages": "9"}}),
        json.dumps({
            "tool": "knowledge_write",
            "args": {
                "type": "fact",
                "title": "Omega validation method",
                "body": "The method uses stratified bootstrap validation.",
                "sources": [PDF_METHOD_URL],
                "evidence": [{
                    "claim": "Omega method uses stratified bootstrap validation.",
                    "source_url": PDF_METHOD_URL,
                    "excerpt": "stratified bootstrap validation with 5,000 resamples",
                    "page": 9,
                }],
            },
        }),
        json.dumps({"tool": "done", "args": {"answer": report}}),
    )
    row = run_case(provider, "scripted", case, "source_search", max_turns=8, timeout=30.0)
    assert row["final_done"]
    assert row["used_source_search"]
    assert row["opened_target_page_or_offset"]
    assert row["saved_exact_evidence_snippet"]
    print("self-test passed")
    return 0


def main() -> int:
    choices = (*provider_ids(), "all")
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=choices, help="provider to test")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=DEFAULT_PROFILE,
        help="cheap runs a low-send live probe; full runs every fixture case",
    )
    parser.add_argument("--arm", action="append", default=[], help="arm or comma list; default from profile")
    parser.add_argument("--case", action="append", default=[], help="case name; default from profile")
    parser.add_argument("--max-cases", type=int, default=0, help="limit selected cases for quick live smoke")
    parser.add_argument("--max-turns", type=int, default=0, help="turn cap; default from profile")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-send timeout seconds")
    parser.add_argument("--new-chat-timeout", type=float, default=45.0, help="per-new-chat timeout seconds")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--trace-output",
        type=Path,
        default=None,
        help="live JSON trace path; default is the output path with .trace.json suffix",
    )
    parser.add_argument(
        "--no-live-trace",
        action="store_true",
        help="disable incremental atomic trace writes during live provider runs",
    )
    parser.add_argument(
        "--open-if-missing",
        action="store_true",
        help="allow the probe to open or foreground a provider page; default only attaches to existing tabs",
    )
    parser.add_argument(
        "--no-new-chat",
        action="store_true",
        help="diagnostic only: reuse the current provider page instead of starting a clean chat",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.provider:
        raise SystemExit("--provider is required unless --self-test is used")
    max_turns = _profile_max_turns(args.profile, args.max_turns)
    arms = _selected_arms(args.arm, profile=args.profile)
    cases = _selected_cases(args.case, args.max_cases, profile=args.profile)
    provider_list = provider_ids() if args.provider == "all" else (args.provider,)
    provider_results = []
    all_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    trace = None if args.no_live_trace else LiveTrace(args.trace_output or _trace_output_path(args.output))
    if trace is not None:
        trace.record({
            "event": "run_start",
            "provider": args.provider,
            "profile": args.profile,
            "arms": list(arms),
            "cases": [case.name for case in cases],
            "max_turns": max_turns,
        })
    for provider_id in provider_list:
        result = run_provider(
            provider_id,
            port=args.port,
            open_if_missing=args.open_if_missing,
            no_new_chat=args.no_new_chat,
            arms=arms,
            cases=cases,
            max_turns=max_turns,
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
            trace=trace,
        )
        provider_results.append(result)
        all_rows.extend(result["rows"])
    payload = {
        "probe": "deep_research_core_ab",
        "profile": args.profile,
        "providers": list(provider_list),
        "arms": list(arms),
        "cases": [case.name for case in cases],
        "max_turns": max_turns,
        "open_if_missing": args.open_if_missing,
        "no_new_chat": args.no_new_chat,
        "trace_output": str(trace.path) if trace is not None else "",
        "new_chat_timeout_seconds": args.new_chat_timeout,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "summary": _summarize(all_rows),
        "provider_results": provider_results,
    }
    _write_output(args.output, payload)
    if trace is not None:
        trace.record({
            "event": "run_complete",
            "summary": payload["summary"],
            "output": str(args.output),
        })
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
