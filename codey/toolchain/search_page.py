"""Match-list pagination for grep results.

Keeps ``toolchain/runtime.py`` under its size ceiling: page slicing and the
next-offset footer live here. First-page truncation text stays byte-identical
to the historical message so existing tests and prompts do not drift.
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
    """Slice the streamed page and append the footer (legacy text on page one)."""
    shown = matches[:limit]
    if not (result_limited or offset > 1):
        return shown
    legacy = (
        f"... truncated after {limit} matches; narrow the query or pass a "
        "subdirectory in path to see the rest"
        if result_limited and offset == 1
        else ""
    )
    return [*shown, page_footer(
        query=query,
        path=path,
        offset=offset,
        limit=limit,
        shown=len(shown),
        has_more=result_limited,
        legacy_truncated_line=legacy,
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
    legacy_truncated_line: str = "",
) -> str:
    start = max(1, int(offset or 1))
    end = start + shown - 1 if shown else start - 1
    if start == 1 and legacy_truncated_line:
        # Historical first-page text first (existing tests pin it verbatim),
        # then the machine-readable page hint.
        if not has_more:
            return legacy_truncated_line
        return (
            f"{legacy_truncated_line}\n"
            f"[grep page: results 1-{end}; next offset={end + 1}; "
            f"{next_call_hint(query=query, path=path, offset=end + 1, limit=limit)}]"
        )
    if has_more:
        return (
            f"[grep page: results {start}-{end}; next offset={end + 1}; "
            f"{next_call_hint(query=query, path=path, offset=end + 1, limit=limit)}]"
        )
    if shown:
        return f"[grep page: results {start}-{end}; end of matches]"
    return "[grep page: no matches at this offset; try offset=1]"


__all__ = ["finalize_page", "next_call_hint", "normalize_page_args", "page_footer"]
