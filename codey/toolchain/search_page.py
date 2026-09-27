"""Match-list pagination for grep results.

Keeps ``toolchain/runtime.py`` under its size ceiling: page slicing and the
next-offset footer live here. All truncated/continued pages share one
machine-readable ``[grep page: ...]`` footer.
"""

from __future__ import annotations

import json


def normalize_page_args(offset: object, limit: object, max_results: object, default: int) -> tuple[int, int]:
    """Coerce grep pagination to a safe ``(start, page)`` pair (cold-start pure)."""
    try:
        start = max(1, int(offset or 1))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        start = 1
    page = max_results if max_results is not None else limit
    try:
        page = max(1, min(int(page or default), 100))  # == SEARCH_PAGE_MAX_RESULTS; repair.py pins it
    except (TypeError, ValueError):
        page = default
    return start, page


def finalize_page(
    matches: list[str],
    *,
    result_limited: bool,
    query: str,
    path: str,
    offset: int,
    limit: int,
) -> list[str]:
    """Slice the streamed page and append the unified page footer."""
    shown = matches[:limit]
    if not (result_limited or offset > 1):
        return shown
    return [*shown, page_footer(
        query=query,
        path=path,
        offset=offset,
        limit=limit,
        shown=len(shown),
        has_more=result_limited,
    )]


def next_call_hint(*, query: str, path: str, offset: int, limit: int) -> str:
    payload = json.dumps(
        {"tool": "grep", "args": {"query": query, "path": path, "offset": offset, "limit": limit}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"next call: {payload}"


def page_footer(
    *,
    query: str,
    path: str,
    offset: int,
    limit: int,
    shown: int,
    has_more: bool,
) -> str:
    start = max(1, int(offset or 1))
    end = start + shown - 1 if shown else start - 1
    if has_more:
        return (
            f"[grep page: results {start}-{end}; next offset={end + 1}; "
            f"{next_call_hint(query=query, path=path, offset=end + 1, limit=limit)}]"
        )
    if shown:
        return f"[grep page: results {start}-{end}; end of matches]"
    return "[grep page: no matches at this offset; try offset=1]"


__all__ = ["finalize_page", "next_call_hint", "normalize_page_args", "page_footer"]
