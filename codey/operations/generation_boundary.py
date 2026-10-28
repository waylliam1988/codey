"""Shared durable provider-generation boundary wiring.

Every provider-backed workflow uses the same boundary callback so a provider
settlement, inbox placement, conversation entries and task checkpoint are
committed through one mutation.  Workflows without an admitted durable task
return ``None`` and keep their existing ephemeral behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def durable_generation_boundary_callback(
    *,
    durable: Any,
    session_id: str,
    task_id: str,
    sink: Any = None,
) -> Callable[[str], str | None] | None:
    """Build the callback used by the shared task kernel.

    A pending provider settlement is deliberately consumed only by
    ``settle_provider_effect_at_boundary``.  That call owns the generation
    boundary, placement and task checkpoint in one durable transaction.
    """

    if durable is None:
        return None
    if not callable(getattr(durable, "commit_generation_boundary", None)):
        return None
    if not session_id or not task_id:
        return None
    has_conversation = getattr(durable, "has_conversation", None)
    if not callable(has_conversation) or not has_conversation(session_id):
        return None

    def place_boundary(boundary: str) -> str | None:
        pending = sink.pending_provider_settlement() if sink is not None else None
        if pending is not None:
            settlement, messages = pending
            mutations = getattr(sink, "mutations", None)
            settle_at_boundary = getattr(mutations, "settle_provider_effect_at_boundary", None)
            if not callable(settle_at_boundary):
                raise RuntimeError("durable provider sink cannot settle at generation boundary")
            placed = settle_at_boundary(
                session_id,
                task_id,
                settlement,
                boundary=boundary,
                messages=messages,
            )
        else:
            placed = durable.commit_generation_boundary(
                session_id,
                task_id=task_id,
                at=boundary,
            )
        texts: list[str] = []
        for submission in placed:
            content = getattr(submission, "content", None)
            if not isinstance(content, Mapping):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        return "\n\n".join(texts) or None

    return place_boundary


__all__ = ["durable_generation_boundary_callback"]
