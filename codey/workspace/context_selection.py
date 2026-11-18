"""Pure, atomic selection of already-authorized context source candidates."""
from __future__ import annotations

from dataclasses import dataclass

from codey.workspace.context_admission import MEMORY_TOTAL_BUDGET_CHARS, ContextCandidate


@dataclass(frozen=True)
class ContextSpan:
    """Source-owned character range registered by the context assembler."""

    key: str
    start: int
    end: int


def assemble_context_candidates(sources: tuple[ContextCandidate, ...]) -> tuple[str, tuple[ContextSpan, ...]]:
    """Join candidate blocks and register their exact locations before freezing."""
    parts: list[str] = []
    spans: list[ContextSpan] = []
    offset = 0
    for source in sources:
        if parts:
            offset += 2
        spans.append(ContextSpan(source.key, offset, offset + len(source.text)))
        parts.append(source.text)
        offset += len(source.text)
    return "\n\n".join(parts), tuple(spans)


def _span_start(span: ContextSpan) -> int:
    return span.start


def select_context_sources(sources: tuple[ContextCandidate, ...], *,
                         budget: int = MEMORY_TOTAL_BUDGET_CHARS) -> tuple[ContextCandidate, ...]:
    """Select whole candidates within individual and combined budgets."""
    chosen: list[ContextCandidate] = []
    used = 0
    for source in sources:
        cost = len(source.text) + (2 if chosen else 0)
        if source.text and len(source.text) <= source.budget and used + cost <= budget:
            chosen.append(source)
            used += cost
    return tuple(chosen)


def replace_context_text(text: str, original: tuple[ContextCandidate, ...],
                       selected: tuple[ContextCandidate, ...], *,
                       spans: tuple[ContextSpan, ...] = ()) -> str:
    """Replace frozen blocks before prompt/request freezing, never on the wire."""
    kept = {source.key: source.text for source in selected}
    sources = {source.key: source for source in original}
    registered: set[str] = set()
    chunks: list[str] = []
    offset = 0
    for span in sorted(spans, key=_span_start):
        source = sources.get(span.key)
        if (source is None or span.key in registered or not offset <= span.start <= span.end <= len(text)
                or text[span.start:span.end] != source.text):
            raise ValueError("invalid registered context source span")
        chunks.extend((text[offset:span.start], kept.get(span.key, "")))
        offset = span.end
        registered.add(span.key)
    if any(source.text != kept.get(source.key, "") and source.key not in registered for source in original):
        raise ValueError("context source edit requires a registered span")
    return "".join((*chunks, text[offset:]))
