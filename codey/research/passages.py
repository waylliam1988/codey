"""Immutable, receipt-derived references to text actually shown by a read tool."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict

from codey.research.source_document import OpenedSource, SourceDocument
from codey.types.json_value import JsonObject, canonical_json_object, require_json_integer, require_json_string


class PassageLedger(Protocol):
    @property
    def read_passages(self) -> dict[str, ReadPassage]: ...


class PassageDescriptor(TypedDict):
    """Wire locator into a validated source receipt, in Unicode code points."""

    ref: str
    start: int
    end: int
    source_version: NotRequired[tuple[str, int]]
    evidence_version: NotRequired[tuple[str, int]]


def read_passage_refs(value: object) -> tuple[str, ...]:
    """Decode citation identities from receipt descriptors without mutating them."""
    if not isinstance(value, list):
        raise ValueError("read passage receipts must be an array")
    refs = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("read passage descriptor must be an object")
        refs.append(require_json_string(row.get("ref"), "passage ref"))
    return tuple(refs)


@dataclass(frozen=True)
class ReadPassage:
    ref: str
    source: OpenedSource
    source_text: str
    pages: tuple[tuple[int, str], ...]
    start: int
    end: int
    source_version: tuple[str, int] | None = None
    evidence_version: tuple[str, int] | None = None

    @property
    def excerpt(self) -> str:
        return self.source_text[self.start:self.end]

    @property
    def page(self) -> int | None:
        return next((number for number, start, end in page_body_ranges(self.source_text, self.pages)
                     if start <= self.start < self.end <= end), None)

    def descriptor(self) -> PassageDescriptor:
        row = PassageDescriptor(ref=self.ref, start=self.start, end=self.end)
        if self.source_version is not None:
            row["source_version"] = self.source_version
        if self.evidence_version is not None:
            row["evidence_version"] = self.evidence_version
        return row


def passage_ref(url: str, text_hash: str, start: int, end: int, *,
                source_version: tuple[str, int] | None = None,
                evidence_version: tuple[str, int] | None = None) -> str:
    from codey.knowledge.model import canonical_json

    pin = "\n" + canonical_json([source_version, evidence_version]) if source_version else ""
    digest = hashlib.sha256(f"{url}\n{text_hash}\n{start}:{end}{pin}".encode()).hexdigest()
    return "p" + digest[:12]


_SENTENCE_END = re.compile(r'''[.!?。！？]["'’”）)\]】]*[ \t\r\n]+''')
MAX_READ_PASSAGES = 32


def preceding_context_offset(text: str, offset: int) -> int:
    """A search locator opens the preceding paragraph/sentence, within 600 chars."""
    lower = max(0, offset - 600)
    prefix = text[lower:offset]
    paragraphs = list(re.finditer(r"\n[ \t\r]*\n", prefix))
    if paragraphs:
        return lower + paragraphs[-1].end()
    sentences = list(_SENTENCE_END.finditer(prefix))
    if sentences:
        return lower + sentences[-1].end()
    return 0 if lower == 0 else offset


def describe_window(url: str, text_hash: str, text: str, offset: int, limit: int) -> list[PassageDescriptor]:
    """Split a visible window at paragraph boundaries, preserving exact substrings."""
    end = min(len(text), offset + limit)
    if end < len(text):
        # Do not offer a final, clipped clause as a complete selectable passage
        # when a complete sentence fits. The caller resumes at the actual end.
        boundaries = list(_SENTENCE_END.finditer(text, offset, end))
        if boundaries:
            end = boundaries[-1].end()
    rows: list[PassageDescriptor] = []
    start = min(offset, len(text))
    while start < end:
        stop = min(end, start + 1000)
        if stop < end:
            # Prefer a complete paragraph, then a complete sentence. Never
            # extend past the bytes actually shown by the read operation.
            window = text[start + 350:stop]
            boundaries = list(re.finditer(r"\n\s*\n", window))
            if not boundaries:
                boundaries = list(re.finditer(r"[.!?。！？](?:[ \t\n]+|$)", window))
            if not boundaries:
                boundaries = list(re.finditer(r"\n", window))
            if boundaries:
                stop = start + 350 + boundaries[-1].end()
        if text[start:stop].strip():
            rows.append({"ref": passage_ref(url, text_hash, start, stop), "start": start, "end": stop})
        start = stop
    return rows


def page_body_ranges(text: str, pages: tuple[tuple[int, str], ...]) -> list[tuple[int, int, int]]:
    """Locate ordered canonical page bodies, including repeated page text."""
    cursor = 0
    ranges = []
    for number, body in pages:
        start = text.find(body, cursor) if body else -1
        if start >= 0:
            cursor = start + len(body)
            ranges.append((number, start, cursor))
    return ranges


def describe_document_window(document: SourceDocument, text_hash: str, offset: int, limit: int, *,
                             coherent: bool = False) -> list[PassageDescriptor]:
    """PDF passages select exact page bodies, excluding generated page labels.

    Keep coordinates in the original document so receipt restoration still
    validates against the same source bytes and version. Split at page boundaries
    even when a short document fits in one model window.
    """
    describe = describe_section_window if coherent else describe_window
    lower, upper = document.read_bounds
    offset = max(lower, offset)
    limit = min(limit, upper - offset)
    if document.content_kind != "pdf":
        # Segmentation sees the end of the selected range as a real boundary;
        # offsets and passage identities still address the full original source.
        return describe(document.final_url, text_hash, document.text[:upper], offset, limit)
    end = min(len(document.text), offset + limit)
    rows: list[PassageDescriptor] = []
    pages = tuple((page.number, page.text) for page in document.page_texts)
    for _number, start, stop in page_body_ranges(document.text, pages):
        visible_start, visible_end = max(offset, start), min(end, stop)
        if visible_start < visible_end:
            rows.extend(describe(document.final_url, text_hash, document.text,
                                 visible_start, visible_end - visible_start))
    return rows


def describe_captured_window(observation: JsonObject, offset: int, limit: int, *,
                             coherent: bool = False) -> list[PassageDescriptor]:
    """Generate web, restored and stored-source windows from the same receipt."""
    from codey.research.source_observation import snapshot_document, source_observation

    source, _text, _pages = source_observation(observation)
    return describe_document_window(snapshot_document(observation), source.text_hash, offset, limit, coherent=coherent)


def describe_section_window(url: str, text_hash: str, text: str, offset: int, limit: int) -> list[PassageDescriptor]:
    """Keep a displayed section together; never extend the actual read window.

    The original receipt still owns exact source offsets. This is structural
    grouping, not a claim that every statement in a section supports an answer.
    """
    original = describe_window(url, text_hash, text, offset, limit)
    if not original:
        return []
    start, end = original[0]["start"], original[-1]["end"]
    boundaries = [start, *(match.start() for match in re.finditer(r"(?m)^#{1,6} .+$", text)
                           if start < match.start() < end), end]
    rows: list[PassageDescriptor] = []
    for begin, stop in zip(boundaries, boundaries[1:], strict=False):
        if not text[begin:stop].strip():
            continue
        # Each original tool window is bounded by the normal acquisition limit.
        rows.append({"ref": passage_ref(url, text_hash, begin, stop), "start": begin, "end": stop})
    return rows


def render_passage_window(document: SourceDocument, rows: list[PassageDescriptor]) -> str:
    """Show PDF page labels as metadata outside the selected original excerpt."""
    ranges = page_body_ranges(document.text, tuple((page.number, page.text) for page in document.page_texts))
    return "\n".join(
        f"passage_ref={row['ref']}" + next((f" [page {number}]" for number, start, end in ranges
            if start <= row["start"] < row["end"] <= end), "")
        + "\n" + document.text[row["start"]:row["end"]] for row in rows)


def receipt_passages(observation: object, descriptors: object) -> tuple[ReadPassage, ...]:
    """Decode immutable bindings from the source receipt without publishing them."""
    from codey.research.source_observation import source_observation

    source, text, pages = source_observation(observation)
    lower, upper = source.read_scope.bounds(text) if source.read_scope is not None else (0, len(text))
    if not isinstance(descriptors, (list, tuple)) or len(descriptors) > MAX_READ_PASSAGES:
        raise ValueError("invalid read passage descriptors")
    decoded = canonical_json_object({"passages": descriptors})["passages"]
    assert isinstance(decoded, list)
    passages: list[ReadPassage] = []
    for row in decoded:
        if (not isinstance(row, dict) or not {"ref", "start", "end"} <= set(row)
                or set(row) - {"ref", "start", "end", "source_version", "evidence_version"}):
            raise ValueError("invalid read passage descriptor")
        start, end = row["start"], row["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("invalid read passage range")
        if not lower <= start < end <= upper:
            raise ValueError("read passage outside captured target range")
        versions: dict[str, tuple[str, int]] = {}
        for key in ("source_version", "evidence_version"):
            if key not in row:
                continue
            from codey.knowledge.model import KnowledgeReference

            pin = row[key]
            if not isinstance(pin, (list, tuple)) or len(pin) != 2:
                raise ValueError("invalid stored material version")
            identity = require_json_string(pin[0], "stored material id")
            revision = require_json_integer(pin[1], "stored material revision")
            KnowledgeReference(identity, revision, "uses")
            versions[key] = (identity, revision)
        if "evidence_version" in versions and "source_version" not in versions:
            raise ValueError("stored evidence requires a pinned source")
        ref = passage_ref(source.final_url, source.text_hash, start, end, **versions)
        if row["ref"] != ref or not text[start:end].strip():
            raise ValueError("read passage identity differs from source version")
        passages.append(ReadPassage(ref, source, text, tuple(sorted(pages.items())), start, end, **versions))
    return tuple(passages)


def restore_passages(ledger: PassageLedger, observation: object, descriptors: object) -> None:
    """Rebuild immutable bindings from the same source receipt, never a live URL."""
    pending: dict[str, ReadPassage] = {}
    for value in receipt_passages(observation, descriptors):
        previous = ledger.read_passages.get(value.ref)
        # Reacquisition time may differ, while the referenced bytes stay identical.
        if previous is not None and (previous.source.final_url, previous.source.text_hash,
                                     previous.source_text, previous.start, previous.end) != (
                value.source.final_url, value.source.text_hash, value.source_text, value.start, value.end):
            raise ValueError("conflicting read passage identity")
        pending[value.ref] = previous or value
    ledger.read_passages.update(pending)


def passage_evidence_valid(ledger: PassageLedger, url: str, excerpt: str, locator: str) -> bool:
    if not locator.startswith("passage:"):
        return False
    passage = ledger.read_passages.get(locator.removeprefix("passage:"))
    return bool(passage and passage.source.final_url == url and passage.excerpt.strip() == excerpt)
