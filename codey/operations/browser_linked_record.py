"""Join an observed list record, its delivered link and a settled detail read."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.automation.browser_document import document_changed, normalize_element_ref, same_document
from codey.automation.browser_receipts import PAGE_EVIDENCE_INVALIDATORS, committed_results, page_effect_possible
from codey.operations.chat_citations import _source_link
from codey.research.passages import receipt_passages
from codey.runtime.core.models import ToolCall
from codey.task.instruction_text import instruction_text
from codey.task.web_intent import linked_record_entry, source_url_key
from codey.types.json_value import JsonObject, decode_json_object

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult

_DATE_PLACE = re.compile(r"(?im)^\s*(?:born|birth)\s*:\s*([^\n]+?)\s+in\s+([^\n]+?)\s*$")
_DATE_ONLY = re.compile(r"(?im)^\s*(?:date of birth|birth date)\s*:\s*([^\n]+?)\s*$")
_PLACE_ONLY = re.compile(r"(?im)^\s*(?:place of birth|birthplace)\s*:\s*([^\n]+?)\s*$")
_HEADING = re.compile(r"(?m)^#{1,6}\s+([^\n]+)")
_BIRTH = re.compile(r"\b(?:born|birth(?:day|place| date)?)\b", re.I)
_TITLE = re.compile(r"\b(?:title|name)\b", re.I)
_TITLE_AND_LINK = re.compile(r"\b(?:title|name)\s+and\s+(?:(?:its|the)(?: page)?\s+)?(?:link|url)\b", re.I)
_INTERPRETATION = re.compile(r"\b(?:why|explain|interpret|compare|opinion|recommend|evaluate|summarize)\b", re.I)


@dataclass(frozen=True)
class LinkedRecordRequest:
    url: str
    quotation: bool
    first_sentence: bool
    author: bool
    date: bool
    place: bool
    title: bool
    link: bool
    render: bool


def requested_linked_record(session: TaskSession) -> LinkedRecordRequest | None:
    from codey.operations.project_completion_checks import _task_requires_modification

    if (session.task_kind not in {"chat", "hybrid"} or session.policy.strict_research
            or session.policy.required_checks or _task_requires_modification(session)
            or not all(session.policy.allows(name) for name in ("web.read", "browser.observe", "browser.interact"))):
        return None
    entry = linked_record_entry(session.task_text)
    if entry is None:
        return None
    url, quotation = entry
    words = instruction_text(session.task_text)
    birth = quotation and _BIRTH.search(words) is not None
    first_sentence = quotation and re.search(r"\bfirst sentence\b", words, re.I) is not None
    author = quotation and re.search(r"\b(?:author|who wrote)\b", words, re.I) is not None
    date = birth and re.search(r"\b(?:when|date|birthday)\b", words, re.I) is not None
    place = birth and re.search(r"\b(?:where|place|birthplace)\b", words, re.I) is not None
    title = not quotation and _TITLE.search(words) is not None
    link = re.search(r"\b(?:include|give|provide|report|return|list|with|and)\b[^.!?;\n]{0,60}\b(?:link|url)\b", words, re.I) is not None
    literal_fields = ((quotation and first_sentence and author and birth and (date or place))
                      or (title and _TITLE_AND_LINK.search(words) is not None))
    render = literal_fields and _INTERPRETATION.search(words) is None
    return LinkedRecordRequest(url, quotation, first_sentence, author, date, place, title, link, render)


@dataclass(frozen=True)
class LinkedRecord:
    snapshot_ref: str
    document: JsonObject
    region: JsonObject
    links: tuple[JsonObject, ...]

    @property
    def link_ref(self) -> str:
        return str(self.links[0]["ref"]) if len(self.links) == 1 else ""

    def contains(self, ref: str) -> bool:
        return any(row["ref"] == normalize_element_ref(ref) for row in self.links)


def _is_entry_snapshot(request: LinkedRecordRequest, value: JsonObject) -> bool:
    scope = value.get("scope")
    return (isinstance(url := value.get("page_url"), str) and source_url_key(url) == source_url_key(request.url)
            and isinstance(scope, dict) and scope.get("root_ref") == "" and scope.get("selector") == "")


def observed_first_record(request: LinkedRecordRequest, snapshot_ref: str,
                          value: JsonObject, document: JsonObject) -> LinkedRecord | None:
    if (not _is_entry_snapshot(request, value) or value.get("elements_truncated") is not False
            or not isinstance(contexts := value.get("contexts"), list)
            or not isinstance(elements := value.get("elements"), list)):
        return None
    regions = [row for row in map(decode_json_object, contexts)
               if type(row.get("ordinal")) is int and row["ordinal"] == 1
               and row.get("kind") == ("quotation" if request.quotation else "record")]
    if len(regions) != 1:
        return None
    region = regions[0]
    links = tuple(row for row in map(decode_json_object, elements)
                  if row.get("role") == "link" and row.get("disabled") is False
                  and row.get("record_link") is True and row.get("context_id") == region.get("id")
                  and isinstance(row.get("ref"), str) and row["ref"])
    return LinkedRecord(snapshot_ref, document, region, links)


@dataclass(frozen=True)
class LinkedRecordProgress:
    request: LinkedRecordRequest
    entry: LinkedRecord | None = None
    click_ref: str = ""
    destination: JsonObject | None = None
    wait_ref: str = ""
    read_ref: str = ""
    text: str = ""
    source_title: str = ""

    def next_call(self, current: JsonObject | None) -> ToolCall | None:
        if self.entry is None:
            return None
        if not self.click_ref and self.entry.link_ref and same_document(self.entry.document, current):
            return ToolCall("browser_click", {"ref": self.entry.link_ref, "purpose": "result"})
        if self.click_ref and not self.wait_ref:
            return ToolCall("browser_wait", {"condition": "new_document", "value": self.click_ref, "timeout_ms": 3000})
        if self.destination is not None and same_document(self.destination, current) and not self.read_ref:
            return ToolCall("browser_read", {})
        return None

    def fields(self) -> tuple[tuple[str, str], ...] | None:
        if self.entry is None or not self.read_ref or self.destination is None:
            return None
        region = self.entry.region
        subject = region.get("subject")
        quotation = region.get("quotation")
        fields: list[tuple[str, str]] = []
        detail = self.text
        if self.request.quotation:
            if (not isinstance(subject, str) or not subject or not isinstance(quotation, str)
                    or (section := _subject_section(detail, subject)) is None):
                return None
            detail = section
            excerpt = quotation.strip('“”"')
            if self.request.first_sentence:
                excerpt = _first_sentence(excerpt)
                if not excerpt:
                    return None
            fields.append(("", excerpt))
            if self.request.author:
                fields.append(("Author", subject))
        if self.request.date or self.request.place:
            joined = tuple(_DATE_PLACE.finditer(detail))
            dates = [match[1] for match in _DATE_ONLY.finditer(detail)] + [match[1] for match in joined]
            places = [match[1] for match in _PLACE_ONLY.finditer(detail)] + [match[2] for match in joined]
            for needed, label, values in ((self.request.date, "Date of birth", dates),
                                           (self.request.place, "Place of birth", places)):
                if needed:
                    distinct = {_space(value) for value in values}
                    if len(distinct) != 1:
                        return None
                    fields.append((label, next(iter(distinct))))
        if self.request.title:
            title = region.get("label")
            headings = {match[1] for match in _HEADING.finditer(self.text)}
            headings.update(self.source_title.split(" | ")[:1])
            if not isinstance(title, str) or not title or _space(title) not in set(map(_space, headings)):
                return None
            fields.append(("Title", title))
        return tuple(fields)


def _space(value: str) -> str:
    return " ".join(value.split())


def _subject_section(text: str, subject: str) -> str | None:
    headings = tuple(_HEADING.finditer(text))
    matches = [index for index, heading in enumerate(headings) if _space(heading[1]) == _space(subject)]
    if len(matches) != 1:
        return None
    index = matches[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
    return text[headings[index].end():end]


def _first_sentence(text: str) -> str:
    for boundary in re.finditer(r"[.!?](?=\s|$)", text):
        prefix = text[:boundary.end()]
        if not re.search(r"\b(?:[A-Z]|Mr|Mrs|Ms|Dr|Prof|St|e\.g|i\.e)\.$", prefix):
            return prefix
    return ""


def _detail_read(receipt: ToolResult, url: str) -> tuple[str, str] | None:
    try:
        rows = receipt_passages(receipt.canonical.get("research_observation"), receipt.canonical.get("read_passages"))
    except (KeyError, ValueError):
        return None
    if not rows or rows[0].source.final_url != url:
        return None
    # Join only delivered spans and remove potentially clipped boundary lines.
    spans: list[str] = []
    for row in rows:
        text = row.excerpt
        if row.start > 0 and row.source_text[row.start - 1] != "\n":
            text = text.partition("\n")[2]
        if ((row.end < len(row.source_text) and row.source_text[row.end] != "\n")
                or (row.end == len(row.source_text) and row.source.truncated and not text.endswith("\n"))):
            text = text.rpartition("\n")[0]
        spans.append(text)
    return "\n\n".join(spans), rows[0].source.title


def linked_record_progress(session: TaskSession, request: LinkedRecordRequest) -> LinkedRecordProgress:
    """A later page transition cannot stand in for the delivered record link."""
    progress = LinkedRecordProgress(request)
    names = PAGE_EVIDENCE_INVALIDATORS | {"browser_snapshot", "browser_wait", "browser_read"}
    for ref, receipt in committed_results(session.executed, session._memory_results, names,
                                         include_failed=PAGE_EVIDENCE_INVALIDATORS):
        name = receipt.call.name
        raw = receipt.canonical.get("value")
        value: JsonObject = {}
        if isinstance(raw, dict):
            value = raw
        document = receipt.audit.get("browser_document")
        if (not progress.click_ref and name == "browser_snapshot" and isinstance(document, dict)
                and _is_entry_snapshot(request, value)):
            progress = replace(progress, entry=observed_first_record(request, ref, value, document))
        elif name in PAGE_EVIDENCE_INVALIDATORS and page_effect_possible(receipt):
            entry = progress.entry
            selector = receipt.call.args.get("ref")
            if (entry is not None and name == "browser_click" and receipt.ok and value.get("delivery") == "delivered"
                    and not progress.click_ref and isinstance(selector, str) and entry.contains(selector)
                    and same_document(entry.document, receipt.audit.get("link_origin"))):
                progress = replace(progress, click_ref=ref)
            elif not progress.read_ref:
                progress = LinkedRecordProgress(request)
        elif (progress.entry is not None and progress.click_ref and name == "browser_wait" and not progress.wait_ref
                and receipt.call.args.get("condition") == "new_document" and receipt.call.args.get("value") == progress.click_ref
                and value.get("matched") is True and isinstance(destination := value.get("document"), dict)
                and destination.get("target_id") == progress.entry.document.get("target_id")
                and destination.get("frame_id") == progress.entry.document.get("frame_id")
                and document_changed(progress.entry.document, destination)):
            progress = replace(progress, destination=destination, wait_ref=ref)
        elif (progress.destination is not None and name == "browser_read" and same_document(progress.destination, document)
                and isinstance(url := progress.destination.get("url"), str) and not progress.read_ref
                and (detail := _detail_read(receipt, url)) is not None):
            progress = replace(progress, read_ref=ref, text=detail[0], source_title=detail[1])
    return progress


def linked_record_completion(session: TaskSession, draft: str) -> tuple[str | None, tuple[str, ...]]:
    if (request := requested_linked_record(session)) is None:
        return draft, ()
    progress = linked_record_progress(session, request)
    if (progress.entry is None or progress.destination is None or (fields := progress.fields()) is None
            or not isinstance(url := progress.destination.get("url"), str)):
        return None, ()
    refs = (progress.entry.snapshot_ref, progress.click_ref, progress.wait_ref, progress.read_ref)
    if request.render:
        lines = [f"{label} — {value}" if label else f"“{value}”" for label, value in fields]
        if request.link:
            lines.append(_source_link("Linked detail page", url))
        return "\n\n".join(lines), refs
    if any(_space(value) not in _space(draft) for _, value in fields) or (request.link and url not in draft):
        return None, ()
    return draft, refs


def linked_record_click_allowed(session: TaskSession, ref: str, document: JsonObject | None) -> bool:
    request = requested_linked_record(session)
    if request is None:
        return True
    progress = linked_record_progress(session, request)
    entry = progress.entry
    if entry is None or not same_document(entry.document, document):
        return True
    return not entry.link_ref or (entry.contains(ref) and not progress.click_ref)
