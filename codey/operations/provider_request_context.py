"""Frozen kernel authority accompanying a finite provider request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    encode_transport_value,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)
from codey.workspace.context_admission import GhostContextSource

if TYPE_CHECKING:
    from codey.operations.kernel_protocol import TurnSnapshot


@dataclass(frozen=True)
class ProviderRequestContext:
    logical_turn: int
    snapshot: TurnSnapshot
    window_ref: ProtocolWindowRef | None
    context_text: str
    ghost_sources: tuple[GhostContextSource, ...]
    effect_scope: str = ""
    close_reply: bool = False
    delivery_purpose: str = ""
    work_ref: str = ""
    source_response_ref: str = ""

    def to_payload(self) -> JsonObject:
        from codey.operations.provider_receipts import snapshot_description

        return {"logical_turn": self.logical_turn, "snapshot": snapshot_description(self.snapshot),
                "window": decode_json_object(self.window_ref.to_payload()) if self.window_ref is not None else None,
                "context": {"text": self.context_text, "ghost_sources": encode_transport_value(self.ghost_sources)},
                "effect_scope": self.effect_scope, "close_reply": self.close_reply,
                "delivery_purpose": self.delivery_purpose, "work_ref": self.work_ref,
                "source_response_ref": self.source_response_ref}

    @classmethod
    def from_payload(cls, value: object) -> ProviderRequestContext:
        from codey.operations.provider_receipts import restore_ghost_context, restore_snapshot

        row = decode_json_object(value)
        turn = require_json_integer(row.get("logical_turn"), "logical_turn")
        if turn < 1:
            raise ValueError("provider logical turn must be positive")
        text, sources = restore_ghost_context(row.get("context"))
        window = row.get("window")
        return cls(turn, restore_snapshot(row.get("snapshot")),
            None if window is None else ProtocolWindowRef.from_payload(window), text, sources,
            require_json_string(row.get("effect_scope"), "effect_scope"),
            require_json_boolean(row.get("close_reply"), "close_reply"),
            require_json_string(row.get("delivery_purpose", ""), "delivery_purpose"),
            require_json_string(row.get("work_ref", ""), "work_ref"),
            require_json_string(row.get("source_response_ref", ""), "source_response_ref"))
