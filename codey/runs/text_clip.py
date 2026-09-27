"""Shared bounded-text clipping for run records.

Single source for the `...`-suffix clipping historically triplicated in
`ledger`, `details`, and `trace`. Semantics are pinned: normalize CRLF/CR,
strip, return a raw prefix when `limit` cannot fit the suffix, otherwise
reserve the suffix and rstrip the kept head before appending it.

This is intentionally not `codey.utils.text_budget.clip_tail`: `clip_tail`
uses a `\\n[truncated]` marker and returns a marker prefix on tiny limits,
while run records keep the historical `...` raw-prefix behavior.
"""

from __future__ import annotations

TRUNCATED_TEXT_SUFFIX = "..."


def clip_text(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX
