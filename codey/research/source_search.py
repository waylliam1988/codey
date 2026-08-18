"""Deterministic locator search within already-opened Research sources."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

SOURCE_SEARCH_DEFAULT_LIMIT = 6
SOURCE_SEARCH_MAX_LIMIT = 12
SOURCE_SEARCH_SNIPPET_CHARS = 320
SOURCE_SEARCH_MAX_TOKEN_HITS = 64
_TOKEN_RE = re.compile(r"[A-Za-z0-9$._/-]{3,}|[\u4e00-\u9fff]{2,}")


@dataclass(frozen=True)
class SourceSearchHit:
    offset: int = 0
    snippet: str = ""
    page: int | None = None
    score: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def bounded_limit(value: object, default: int = SOURCE_SEARCH_DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(SOURCE_SEARCH_MAX_LIMIT, parsed))


def search_text(text: str, query: str, limit: int = SOURCE_SEARCH_DEFAULT_LIMIT) -> list[SourceSearchHit]:
    tokens = query_tokens(query)
    if not tokens:
        return []
    text = str(text or "")
    if not text:
        return []
    hits: list[SourceSearchHit] = []
    for offset in _candidate_offsets(text, tokens):
        snippet = snippet_at(text, offset)
        hits.append(SourceSearchHit(offset=offset, snippet=snippet, score=_score(snippet, tokens)))
    return _rank_hits(hits, bounded_limit(limit))


def search_pages(
    pages: dict[int, str],
    query: str,
    limit: int = SOURCE_SEARCH_DEFAULT_LIMIT,
) -> list[SourceSearchHit]:
    tokens = query_tokens(query)
    if not tokens:
        return []
    hits: list[SourceSearchHit] = []
    for page in sorted(pages):
        text = str(pages.get(page) or "")
        offsets = _candidate_offsets(text, tokens)
        if not offsets:
            continue
        best_for_page: SourceSearchHit | None = None
        for offset in offsets:
            snippet = snippet_at(text, offset)
            hit = SourceSearchHit(
                offset=offset,
                snippet=snippet,
                page=page,
                score=_score(snippet, tokens),
            )
            if best_for_page is None or (hit.score, -hit.offset) > (
                best_for_page.score,
                -best_for_page.offset,
            ):
                best_for_page = hit
        if best_for_page is not None:
            hits.append(best_for_page)
    return _rank_hits(hits, bounded_limit(limit))


def render_results(final_url: str, hits: list[SourceSearchHit]) -> str:
    if not hits:
        return "no source_search matches"
    lines = [
        "source_search results from an already-opened source.",
        "Locator preview only. Use open_hit from the next allowed-actions block before citing.",
    ]
    for index, hit in enumerate(hits, 1):
        if hit.page is not None:
            lines.append(f'{index}. p.{hit.page}: {hit.snippet}')
        else:
            lines.append(f"{index}. offset {hit.offset}: {hit.snippet}")
    return "\n".join(lines)


def query_tokens(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for item in _TOKEN_RE.findall(str(query or "").lower()):
        cleaned = item.strip("._/-")
        if cleaned and cleaned not in tokens:
            tokens.append(cleaned)
        if len(tokens) >= 8:
            break
    return tuple(tokens)


def snippet_at(text: str, offset: int) -> str:
    text = str(text or "")
    offset = max(0, min(len(text), int(offset or 0)))
    start = max(0, offset - 130)
    return _clip(" ".join(text[start : start + SOURCE_SEARCH_SNIPPET_CHARS].split()), SOURCE_SEARCH_SNIPPET_CHARS)


def _candidate_offsets(text: str, tokens: tuple[str, ...]) -> list[int]:
    lowered = text.lower()
    offsets: list[int] = []
    for token in tokens:
        start = 0
        token_hits = 0
        while token_hits < SOURCE_SEARCH_MAX_TOKEN_HITS:
            found = lowered.find(token, start)
            if found < 0:
                break
            offsets.append(found)
            token_hits += 1
            start = found + max(1, len(token))
    return sorted(set(offsets))


def _score(snippet: str, tokens: tuple[str, ...]) -> int:
    lowered = snippet.lower()
    return sum(1 for token in tokens if token in lowered)


def _rank_hits(hits: list[SourceSearchHit], limit: int) -> list[SourceSearchHit]:
    ranked = sorted(hits, key=lambda hit: (-hit.score, hit.page or 0, hit.offset))
    return ranked[:limit]


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: max(0, limit - 3)].rstrip() + "..."
