"""Provider-neutral context maintenance. Only validated views may be committed."""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from codey.providers.api_codec import ApiCodec
from codey.providers.base import ProviderToolDefinition
from codey.providers.context_checkpoint import (
    ContextRange,
    reduce_old_outputs,
    replace_range,
    select_range,
    validate_summary,
)
from codey.providers.context_ledger import digest
from codey.providers.token_accounting import RequestContextCount

Items = list[dict[str, Any]]
Tools = list[ProviderToolDefinition] | None


@dataclass(frozen=True)
class ContextSnapshot:
    items: Items
    generation: int
    identity: str
    result_refs: frozenset[str]
    checkpoints: frozenset[str] = frozenset()


class CompactionCoordinator:
    def __init__(self, *, codec: ApiCodec, snapshot: Callable[[], ContextSnapshot],
                 count: Callable[[Items, Tools, float], RequestContextCount],
                 commit: Callable[[ContextRange, Items, dict[str, Any], ContextSnapshot, Tools, float], bool],
                 originals: Callable[[Items, list[dict[str, Any]]], Items], summarize: Callable[[Items, float], str],
                 recent_budget: Callable[[], int]) -> None:
        self.codec, self.snapshot, self.count = codec, snapshot, count
        self.commit, self.originals, self.summarize = commit, originals, summarize
        self.recent_budget = recent_budget
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.diagnostics: list[dict[str, Any]] = []

    def compact(self, items: Items, tools: Tools, deadline: float, snapshot: ContextSnapshot, *,
                merge_checkpoints: bool = False, staged: list[dict[str, Any]] | None = None) -> Items | None:
        if self.snapshot().generation != snapshot.generation:
            return None
        started = time.time()
        reduced = reduce_old_outputs(self.codec, items, snapshot.result_refs)
        if reduced != items:
            selected = ContextRange(0, len(items), digest(items))
            replacement = reduced
            checkpoint: dict[str, Any] = {"kind": "output_reduction", "source": items}
        else:
            starts = [start for start, _ in self.codec.closed_spans(items)]
            low, high = 0, len(starts)
            while low < high:
                middle = (low + high) // 2
                measured = self.count(items[starts[middle]:], tools, deadline)
                if measured.value is None:
                    raise ValueError("recent context count is unavailable")
                if measured.value <= self.recent_budget():
                    high = middle
                else:
                    low = middle + 1
            recent_start = starts[low] if low < len(starts) else len(items)
            span = select_range(self.codec, items, snapshot.checkpoints, merge_checkpoints=merge_checkpoints,
                                recent_start=recent_start)
            if span is None:
                return None
            selected = span
            source = items[selected.start:selected.end]
            summary = validate_summary(self.summarize(self.originals(source, staged or []), deadline))
            item = self.codec.checkpoint_item(f"State record: work-{selected.source_digest[:24]}\n{summary}")
            replacement = [item]
            checkpoint = {"kind": "work_state", "source": source, "item_digest": digest(item), "text": summary}
        candidate = replace_range(items, selected, replacement)
        self.codec.validate_view(candidate)
        before, after = self.count(items, tools, deadline), self.count(candidate, tools, deadline)
        if before.value is None or after.value is None or after.value >= before.value:
            from codey.providers.error_classification import ContextOverflowError

            raise ContextOverflowError("work state does not reduce request size")
        checkpoint.update(source_digest=selected.source_digest, start=selected.start, end=selected.end,
                          before=asdict(before), after=asdict(after), model_identity=snapshot.identity,
                          started_at=started, finished_at=time.time())
        committed = False
        if staged is None:
            committed = self.commit(selected, replacement, checkpoint, snapshot, tools, deadline)
        else:
            staged.append(checkpoint)
        self.diagnostics.append({key: value for key, value in checkpoint.items() if key not in {"source", "item_digest", "text"}} | {"committed": committed})
        del self.diagnostics[:-32]
        return candidate

    def accept(self, checkpoints: list[dict[str, Any]]) -> None:
        """Mark staged diagnostics only after their durable transaction commits."""
        accepted = {(row["source_digest"], row["started_at"]) for row in checkpoints}
        for row in self.diagnostics:
            if (row.get("source_digest"), row.get("started_at")) in accepted:
                row["committed"] = True

    def schedule(self, tools: Tools, timeout: float) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            snapshot = self.snapshot()

            def maintain() -> None:
                try:
                    self.compact(copy.deepcopy(snapshot.items), tools, time.monotonic() + timeout, snapshot)
                except Exception as exc:
                    self.diagnostics.append({"source_digest": digest(snapshot.items), "committed": False,
                                             "error": str(exc)[:300], "finished_at": time.time()})
                    del self.diagnostics[:-32]

            self._thread = threading.Thread(target=maintain, name="codey-context-maintenance", daemon=True)
            self._thread.start()

    def wait(self, timeout: float) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
