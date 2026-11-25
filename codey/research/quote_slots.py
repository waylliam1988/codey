"""Expand exact quotations from bounded, verified source passages."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from codey.research.passages import ReadPassage
from codey.types.json_value import JsonValue
from codey.utils.citation_scanner import mask_markdown_code

_SLOT = re.compile(r"\{\{quote:([^{}\s]{1,320})\}\}")
_FENCE = re.compile(r"`+")
_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n")
_EXACT_QUOTE = re.compile(r"\b(?:quote|verbatim)\b|逐字|原文引用", re.I)


def requests_exact_quote(task: str) -> bool:
    return _EXACT_QUOTE.search(task) is not None


def quote_slot(reference: str) -> str:
    return "{{quote:" + reference + "}}"


def paragraph_quote_slots(reference: str, passage: ReadPassage) -> list[JsonValue]:
    excerpt = passage.excerpt
    starts = [0]
    stops: list[int] = []
    for match in _PARAGRAPH_BREAK.finditer(excerpt):
        stops.append(match.start())
        starts.append(match.end())
    stops.append(len(excerpt))
    choices: list[JsonValue] = [
        {"excerpt": excerpt[start:stop][:120],
         "slot": quote_slot(f"{reference}:{passage.start + start}:{passage.start + stop}")}
        for start, stop in zip(starts, stops, strict=True) if excerpt[start:stop].strip()]
    return choices[:12]


def render_quote_slots(text: str, resolve: Callable[[str], tuple[ReadPassage, str]]) -> str:
    masked = mask_markdown_code(text)
    parts: list[str] = []
    offset = 0
    for match in _SLOT.finditer(masked):
        if "{{quote:" in masked[offset:match.start()]:
            raise ValueError("Use a quote slot offered by the original receipt.")
        reference = match[1]
        fields = reference.rsplit(":", 2)
        ranged = len(fields) == 3 and all(re.fullmatch(r"[0-9]{1,8}", part) for part in fields[1:])
        passage, citation = resolve(fields[0] if ranged else reference)
        start, end = (int(fields[1]), int(fields[2])) if ranged else (passage.start, passage.end)
        if not passage.start <= start < end <= passage.end:
            raise ValueError("Quote range must be inside the cited passage.")
        exact = passage.source_text[start:end]
        fence = "`" * max(3, 1 + max((len(token[0]) for token in _FENCE.finditer(exact)), default=0))
        parts.extend((text[offset:match.start()], "\n\n" + fence + "text\n" + exact + "\n" + fence + "\n\n" + citation + "\n\n"))
        offset = match.end()
    tail = text[offset:]
    if "{{quote:" in masked[offset:]:
        raise ValueError("Use a quote slot offered by the original receipt.")
    return ("".join(parts) + tail).strip() if parts else text


def render_research_quotes(text: str, aliases: tuple[tuple[str, str], ...],
                           passages: Mapping[str, ReadPassage]) -> str:
    labels = dict(aliases)

    def resolve(label: str) -> tuple[ReadPassage, str]:
        passage = passages.get(labels.get(label, ""))
        if passage is None:
            raise ValueError("Select a read evidence label for the quote, such as {{quote:e1}}.")
        return passage, f"[{label}]"

    return render_quote_slots(text, resolve)
