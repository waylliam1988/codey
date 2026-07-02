"""Small text-budget helpers shared by user-approved and controlled commands."""

from __future__ import annotations


OUTPUT_OMISSION_MARKER = "\n\n... middle of output omitted ...\n\n"


def clip_middle(
    text: str,
    limit: int,
    marker: str = OUTPUT_OMISSION_MARKER,
) -> tuple[str, bool]:
    """Keep both ends of long text while respecting one total character limit."""
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    if len(marker) >= limit:
        return marker[:limit], True
    available = limit - len(marker)
    head = (available + 1) // 2
    tail = available - head
    clipped = text[:head].rstrip() + marker
    if tail:
        clipped += text[-tail:].lstrip()
    return clipped, True
