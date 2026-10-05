"""Bounded progress observations shared by the task kernel and runaway guard."""

from __future__ import annotations

from dataclasses import dataclass


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
            try:
                max_items = int(max_items)
            except (TypeError, ValueError, OverflowError):
                max_items = 256
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


def seen_info_key(tool_name: str, path: str, model_text: str) -> tuple[str, str, str]:
    import hashlib

    digest = hashlib.sha256(str(model_text or "").encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return (str(tool_name or ""), str(path or ""), digest)


@dataclass(frozen=True)
class ToolAttemptRecord:
    tool: str
    call_fp: str
    result_fp: str
    ok: bool
    changed: bool
    turn: int
    edit_epoch: int = 0


__all__ = ["SeenInfoLRU", "ToolAttemptRecord", "seen_info_key"]
