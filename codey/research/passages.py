"""Immutable, receipt-derived references to text actually shown by a read tool."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.research.ledger import OpenedSource, ResearchLedger
    from codey.research.source_document import SourceDocument


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

    def descriptor(self) -> dict[str, Any]:
        return {"ref": self.ref, "start": self.start, "end": self.end,
                **({"source_version": list(self.source_version)} if self.source_version else {}),
                **({"evidence_version": list(self.evidence_version)} if self.evidence_version else {})}


def passage_ref(url: str, text_hash: str, start: int, end: int, *,
                source_version: Any = None, evidence_version: Any = None) -> str:
    from codey.knowledge.model import canonical_json

    pin = "\n" + canonical_json([source_version, evidence_version]) if source_version else ""
    digest = hashlib.sha256(f"{url}\n{text_hash}\n{start}:{end}{pin}".encode()).hexdigest()
    return "p" + digest[:12]


_SENTENCE_END = re.compile(r'''[.!?。！？]["'’”）)\]】]*[ \t\r\n]+''')


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


def describe_window(url: str, text_hash: str, text: str, offset: int, limit: int) -> list[dict[str, Any]]:
    """Split a visible window at paragraph boundaries, preserving exact substrings."""
    end = min(len(text), offset + limit)
    if end < len(text):
        # Do not offer a final, clipped clause as a complete selectable passage
        # when a complete sentence fits. The caller resumes at the actual end.
        boundaries = list(_SENTENCE_END.finditer(text, offset, end))
        if boundaries:
            end = boundaries[-1].end()
    rows = []
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
                             coherent: bool = False) -> list[dict[str, Any]]:
    """PDF passages select exact page bodies, excluding generated page labels.

    Keep coordinates in the original document so receipt restoration still
    validates against the same source bytes and version. Split at page boundaries
    even when a short document fits in one model window.
    """
    describe = describe_section_window if coherent else describe_window
    if document.content_kind != "pdf":
        return describe(document.final_url, text_hash, document.text, offset, limit)
    end = min(len(document.text), offset + limit)
    rows = []
    pages = tuple((page.number, page.text) for page in document.page_texts)
    for _number, start, stop in page_body_ranges(document.text, pages):
        visible_start, visible_end = max(offset, start), min(end, stop)
        if visible_start < visible_end:
            rows.extend(describe(document.final_url, text_hash, document.text,
                                 visible_start, visible_end - visible_start))
    return rows


def describe_section_window(url: str, text_hash: str, text: str, offset: int, limit: int) -> list[dict[str, Any]]:
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
    rows = []
    for begin, stop in zip(boundaries, boundaries[1:], strict=False):
        if not text[begin:stop].strip():
            continue
        # Each original tool window is bounded by the normal acquisition limit.
        rows.append({"ref": passage_ref(url, text_hash, begin, stop), "start": begin, "end": stop})
    return rows


def render_passage_window(document: SourceDocument, rows: list[dict[str, Any]]) -> str:
    """Show PDF page labels as metadata outside the selected original excerpt."""
    ranges = page_body_ranges(document.text, tuple((page.number, page.text) for page in document.page_texts))
    return "\n".join(
        f"passage_ref={row['ref']}" + next((f" [page {number}]" for number, start, end in ranges
            if start <= row["start"] < row["end"] <= end), "")
        + "\n" + document.text[row["start"]:row["end"]] for row in rows)


def restore_passages(ledger: ResearchLedger, observation: object, descriptors: object) -> None:
    """Rebuild immutable bindings from the same source receipt, never a live URL."""
    from codey.research.ledger_receipts import source_observation

    source, text, pages = source_observation(observation)
    if not isinstance(descriptors, (list, tuple)) or len(descriptors) > 32:
        raise ValueError("invalid read passage descriptors")
    pending = {}
    for row in descriptors:
        if (not isinstance(row, dict) or not {"ref", "start", "end"} <= set(row)
                or set(row) - {"ref", "start", "end", "source_version", "evidence_version"}):
            raise ValueError("invalid read passage descriptor")
        start, end = row["start"], row["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("invalid read passage range")
        versions: dict[str, tuple[str, int]] = {}
        for key in ("source_version", "evidence_version"):
            if key not in row:
                continue
            from codey.knowledge.model import KnowledgeReference

            pin = row[key]
            if not isinstance(pin, (list, tuple)) or len(pin) != 2:
                raise ValueError("invalid stored material version")
            KnowledgeReference(pin[0], pin[1], "uses")
            versions[key] = (pin[0], pin[1])
        if "evidence_version" in versions and "source_version" not in versions:
            raise ValueError("stored evidence requires a pinned source")
        ref = passage_ref(source.final_url, source.text_hash, start, end, **versions)
        if row["ref"] != ref or not text[start:end].strip():
            raise ValueError("read passage identity differs from source version")
        value = ReadPassage(ref, source, text, tuple(sorted(pages.items())), start, end, **versions)
        previous = ledger.read_passages.get(ref)
        # Reacquisition time may differ, while the referenced bytes stay identical.
        if previous is not None and (previous.source.final_url, previous.source.text_hash,
                                     previous.source_text, previous.start, previous.end) != (
                source.final_url, source.text_hash, text, start, end):
            raise ValueError("conflicting read passage identity")
        pending[ref] = previous or value
    ledger.read_passages.update(pending)


def passage_evidence_valid(ledger: ResearchLedger, url: str, excerpt: str, locator: str) -> bool:
    if not locator.startswith("passage:"):
        return False
    passage = ledger.read_passages.get(locator.removeprefix("passage:"))
    return bool(passage and passage.source.final_url == url and passage.excerpt.strip() == excerpt)
