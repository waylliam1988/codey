"""Immutable, receipt-derived references to text actually shown by a read tool."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.research.ledger import OpenedSource, ResearchLedger


@dataclass(frozen=True)
class ReadPassage:
    ref: str
    source: OpenedSource
    source_text: str
    pages: tuple[tuple[int, str], ...]
    start: int
    end: int

    @property
    def excerpt(self) -> str:
        return self.source_text[self.start:self.end]

    def descriptor(self) -> dict[str, Any]:
        return {"ref": self.ref, "start": self.start, "end": self.end}


def passage_ref(url: str, text_hash: str, start: int, end: int) -> str:
    digest = hashlib.sha256(f"{url}\n{text_hash}\n{start}:{end}".encode()).hexdigest()
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


def restore_passages(ledger: ResearchLedger, observation: object, descriptors: object) -> None:
    """Rebuild immutable bindings from the same source receipt, never a live URL."""
    from codey.research.ledger_receipts import source_observation

    source, text, pages = source_observation(observation)
    if not isinstance(descriptors, (list, tuple)) or len(descriptors) > 32:
        raise ValueError("invalid read passage descriptors")
    pending = {}
    for row in descriptors:
        if not isinstance(row, dict) or set(row) != {"ref", "start", "end"}:
            raise ValueError("invalid read passage descriptor")
        start, end = row["start"], row["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("invalid read passage range")
        ref = passage_ref(source.final_url, source.text_hash, start, end)
        if row["ref"] != ref or not text[start:end].strip():
            raise ValueError("read passage identity differs from source version")
        value = ReadPassage(ref, source, text, tuple(sorted(pages.items())), start, end)
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
