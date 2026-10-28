"""Bounded progress observations shared by the task kernel and runaway guard."""

from __future__ import annotations

from codey.runtime.core.progress import ToolAttemptRecord
from codey.utils.refs import parse_int


class SeenInfoLRU:
    """Bounded dedupe for information-tool outputs.

    Keys are ``(tool_name, path, sha256(model_text)[:24])`` so long model
    outputs never accumulate in memory. Insertion-order eviction keeps the
    working set small and cold-start free (no extra deps, no fallback).
    """

    def __init__(self, max_items: int = 256) -> None:
        if isinstance(max_items, bool):
            max_items = 256
        else:
            parsed_items = parse_int(max_items)
            max_items = parsed_items if parsed_items is not None else 256
        self.max_items = max(1, max_items)
        self._order: dict[tuple[str, str, str], None] = {}

    def __contains__(self, key: object) -> bool:
        return key in self._order

    def __len__(self) -> int:
        return len(self._order)

    def add(self, key: tuple[str, str, str]) -> bool:
        if key in self._order:
            return False
        self._order[key] = None
        while len(self._order) > self.max_items:
            self._order.pop(next(iter(self._order)))
        return True

    def clear(self) -> None:
        self._order.clear()

    def keys(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(self._order)


def seen_info_key(tool_name: str, path: str, model_text: str) -> tuple[str, str, str]:
    import hashlib

    digest = hashlib.sha256((model_text or "").encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return ((tool_name or ""), (path or ""), digest)


__all__ = ["SeenInfoLRU", "ToolAttemptRecord", "seen_info_key"]
