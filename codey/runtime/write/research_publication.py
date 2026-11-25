"""Publication facts on the original mutation line, with no extra lifecycle."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.durable.model import ArtifactRecord

from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.effects.effect_records import (
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    prepare_intent,
    prepare_settlement,
)
from codey.runtime.log.session_view import SessionView, require_open_view
from codey.runtime.write.checkpoint import RuntimeChanges

PUBLICATION_TOOL = "_publish_research_answer"


def publication_changes(view: SessionView, *,
                     session_id: str, run_id: str, expected: RuntimeOperationState,
                     intent: RuntimeEffectIntent | None = None,
                     settlement: RuntimeEffectSettlement | None = None,
                     artifact: ArtifactRecord | None = None) -> RuntimeChanges:
    state = require_open_view(view)
    if state != expected or state.leaf != "kernel_settled" or state.task_kind != "research":
        raise RuntimeEffectError("research publication requires its original settled kernel")
    if intent is not None and settlement is not None:
        raise RuntimeEffectError("publication requires exactly one intent or receipt")
    if intent is not None:
        if intent.tool_name != PUBLICATION_TOOL or intent.delivery_batch_id or intent.parent_effect_id:
            raise RuntimeEffectError("invalid research publication intent")
        if (artifact is None or artifact.owner_task_id != run_id or artifact.owner_effect_id != intent.effect_id
                or artifact.handle != intent.display_ref or artifact.metadata.get("kind") != "research_publication"):
            raise RuntimeEffectError("publication intent requires its frozen command artifact")
        if any(row.intent.effect_id == intent.effect_id for row in view.effects):
            raise RuntimeEffectError("research publication identity already admitted")
        if any(row.settlement is None for row in view.effects):
            raise RuntimeEffectError("research publication cannot bypass pending effects")
        return (prepare_intent(session_id, run_id, intent),)
    if settlement is None:
        raise RuntimeEffectError("publication requires exactly one intent or receipt")
    matching = next((row for row in view.effects if row.intent.effect_id == settlement.effect_id), None)
    if matching is None or matching.intent.tool_name != PUBLICATION_TOOL:
        raise RuntimeEffectError("research publication receipt has no original intent")
    return (prepare_settlement(session_id, run_id, settlement, view.effects),)
