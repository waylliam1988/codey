"""Structured generation actions through the existing durable mutation line."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GenerationBoundaryRequest:
    kind: str
    settle_kernel: bool = False
    expected: Any = None


@dataclass(frozen=True)
class GenerationBoundaryResult:
    kind: str
    provider_effect_id: str
    submission_ids: tuple[str, ...]
    conversation_revision: int
    text: str


def durable_generation_boundary_callback(
    *, durable: Any, session_id: str, task_id: str, sink: Any = None,
) -> Callable[[GenerationBoundaryRequest], GenerationBoundaryResult]:
    if durable is None or sink is None or not session_id or not task_id:
        raise ValueError("generation actions require an admitted durable task and effect sink")

    def commit(request: GenerationBoundaryRequest) -> GenerationBoundaryResult:
        pending = sink.pending_provider_settlement()
        effect_id = pending[0].effect_id if pending is not None else ""
        if request.settle_kernel:
            placed = sink.mutations.settle_kernel_boundary(session_id, task_id, expected=request.expected,
                settlement=pending[0] if pending is not None else None, boundary=request.kind)
        elif pending is not None:
            placed = sink.mutations.settle_provider_effect_at_boundary(session_id, task_id, pending[0],
                                                                       boundary=request.kind, messages=pending[1])
        else:
            raise ValueError("generation action has no pending provider response")
        texts = [item.content["text"].strip() for item in placed if isinstance(item.content, Mapping)
                 and isinstance(item.content.get("text"), str) and item.content["text"].strip()]
        _, revision = durable.head(session_id)
        return GenerationBoundaryResult(request.kind, effect_id, tuple(item.submission_id for item in placed),
                                        revision, "\n\n".join(texts))
    return commit
