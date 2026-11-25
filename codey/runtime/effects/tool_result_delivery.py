"""Runtime tool result delivery tracking, receipts, and projection.

The durable session owns task/effect recovery facts. This module records bounded receipts
around the delivery of tool results to model providers:
1. batch_intent: recorded before tool execution in a turn, capturing all planned results
2. send_attempt: recorded when a provider send effect starts
3. send_superseded: voids a prior attempt that deterministically sent nothing
   usable (e.g. a context-overflow rejection), allowing one live retry
4. delivered: recorded when the provider request settles successfully
5. recovered: results reconstructed on resume; delivery is still pending

Strictly bounded: never records raw result text, prompts, replies, stdout,
stderr, diffs, or source bodies.

Does not import agents, provider, ghost, operations, or tool runners.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from codey.durable.model import TaskCheckpoint, thaw_json
from codey.runtime.core.operation_payload import RuntimeOperationTransitionError
from codey.runtime.core.operation_state import lane_for_run, operation_id_for_run
from codey.runtime.effects.replay_policy import is_replayable_safe_tool
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    canonical_json_object,
    decode_json_object,
    require_json_boolean,
    require_json_string,
    require_json_strings,
)

if TYPE_CHECKING:
    from codey.durable.session import DurableSession

SCHEMA_VERSION = 1
EFFECT_KIND = "tool_result_delivery"

RECORD_KIND_BATCH_INTENT = "batch_intent"
ABANDONED_REASONS = frozenset({"writer_settled", "repair_settled", "operation_terminal"})

MAX_BATCH_ID_CHARS = 128
MAX_EFFECT_ID_CHARS = 128
MAX_DIGEST_CHARS = 64
MAX_REF_CHARS = 160
MAX_TOOL_REFS = 64

FORBIDDEN_RAW_KEYS = frozenset({
    "prompt",
    "reply",
    "raw_prompt",
    "raw_reply",
    "result",
    "results",
    "raw_result",
    "raw_results",
    "stdout",
    "stderr",
    "diff",
    "source",
    "source_body",
    "content",
    "body",
    "text",
})

_ITEM_PAYLOAD_KEYS = frozenset({
    "tool_index",
    "tool_name",
    "ref",
    "replay_class",
    "is_denied",
})

_INTENT_PAYLOAD_KEYS = frozenset({
    "schema_version",
    "effect_kind",
    "record_kind",
    "ref",
    "batch_id",
    "session_id",
    "run_id",
    "lane",
    "operation_id",
    "turn",
    "items",
    "batch_digest",
    "created_at",
})

class ToolResultDeliveryError(ValueError):
    """Raised when delivery receipt data violates schema, bounds, or invariants."""


@dataclass(frozen=True)
class DeliveryCommit:
    """Witness that a provider accepted one complete result batch.

    The durable mutation commits this receipt with the provider settlement.
    """

    window_ref: Mapping[str, object]
    batch_id: str
    provider_effect_id: str
    results_digest: str
    invocation_kind: str
    reply_status: str
    input_digest: str = ""
    output_digest: str = ""

    def validate(self) -> None:
        if not isinstance(self.window_ref, Mapping) or not self.window_ref:
            raise ToolResultDeliveryError("window_ref must be a non-empty mapping")
        _require_bounded_str(self.batch_id, "batch_id", MAX_BATCH_ID_CHARS)
        _require_bounded_str(self.provider_effect_id, "provider_effect_id", MAX_EFFECT_ID_CHARS)
        _require_bounded_str(self.results_digest, "results_digest", MAX_DIGEST_CHARS)
        _require_bounded_str(self.invocation_kind, "invocation_kind", 64)
        _require_bounded_str(self.reply_status, "reply_status", 64)
        for name, value in (("input_digest", self.input_digest), ("output_digest", self.output_digest)):
            if value:
                _require_bounded_str(value, name, MAX_DIGEST_CHARS)

    def to_payload(self) -> JsonObject:
        self.validate()
        return {
            "window_ref": canonical_json_object(self.window_ref),
            "batch_id": self.batch_id,
            "provider_effect_id": self.provider_effect_id,
            "results_digest": self.results_digest,
            "invocation_kind": self.invocation_kind,
            "reply_status": self.reply_status,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> DeliveryCommit:
        expected = {"window_ref", "batch_id", "provider_effect_id", "results_digest",
                    "invocation_kind", "reply_status", "input_digest", "output_digest"}
        if set(payload) != expected:
            raise ToolResultDeliveryError(f"delivery commit keys mismatch: {set(payload) ^ expected}")
        value = cls(
            window_ref=decode_json_object(payload["window_ref"]),
            batch_id=require_json_string(payload["batch_id"], "batch_id"),
            provider_effect_id=require_json_string(payload["provider_effect_id"], "provider_effect_id"),
            results_digest=require_json_string(payload["results_digest"], "results_digest"),
            invocation_kind=require_json_string(payload["invocation_kind"], "invocation_kind"),
            reply_status=require_json_string(payload["reply_status"], "reply_status"),
            input_digest=require_json_string(payload["input_digest"], "input_digest"),
            output_digest=require_json_string(payload["output_digest"], "output_digest"),
        )
        value.validate()
        return value


def _require_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ToolResultDeliveryError(f"{name} must be a non-negative int, got {value!r}")
    return value


def _require_bounded_str(value: object, name: str, max_chars: int) -> str:
    if not isinstance(value, str) or type(value) is not str:
        raise ToolResultDeliveryError(f"{name} must be a str, got {type(value)}")
    val = value.strip()
    if not val:
        raise ToolResultDeliveryError(f"{name} must not be empty")
    if len(val) > max_chars:
        raise ToolResultDeliveryError(f"{name} exceeds max length {max_chars}: {len(val)}")
    return val


@dataclass(frozen=True)
class DeliveryBatchItem:
    tool_index: int
    tool_name: str
    ref: str
    replay_class: str
    is_denied: bool = False

    def validate(self) -> None:
        _require_nonnegative_int(self.tool_index, "tool_index")
        _require_bounded_str(self.tool_name, "tool_name", 128)
        _require_bounded_str(self.ref, "ref", MAX_REF_CHARS)
        if self.replay_class not in {"safe", "unsafe"}:
            raise ToolResultDeliveryError(f"invalid replay_class: {self.replay_class!r}")
        if type(self.is_denied) is not bool:
            raise ToolResultDeliveryError(f"invalid is_denied: {self.is_denied!r}")

    def to_dict(self) -> JsonObject:
        self.validate()
        return {
            "tool_index": self.tool_index,
            "tool_name": self.tool_name,
            "ref": self.ref,
            "replay_class": self.replay_class,
            "is_denied": self.is_denied,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> DeliveryBatchItem:
        if not isinstance(data, Mapping):
            raise ToolResultDeliveryError(f"invalid item payload type: {type(data)}")
        if set(data.keys()) != _ITEM_PAYLOAD_KEYS:
            diff = set(data.keys()) ^ _ITEM_PAYLOAD_KEYS
            raise ToolResultDeliveryError(f"item payload key mismatch: {diff}")
        item = cls(
            tool_index=_require_nonnegative_int(data["tool_index"], "tool_index"),
            tool_name=require_json_string(data["tool_name"], "tool_name"),
            ref=require_json_string(data["ref"], "ref"),
            replay_class=require_json_string(data["replay_class"], "replay_class"),
            is_denied=require_json_boolean(data["is_denied"], "is_denied"),
        )
        item.validate()
        return item


def compute_batch_digest(items: tuple[DeliveryBatchItem, ...]) -> str:
    if not items:
        raise ToolResultDeliveryError("items must not be empty for digest computation")
    parts = [
        f"{it.tool_index}:{it.tool_name}:{it.ref}:{it.replay_class}:{1 if it.is_denied else 0}"
        for it in items
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def new_batch_id(run_id: str, turn: int) -> str:
    clean_run = "".join(c for c in run_id if c.isalnum() or c in "-_")[:40]
    token = uuid.uuid4().hex[:12]
    return f"deliv-{clean_run}-t{turn}-{token}"


def _check_no_forbidden_keys(payload: JsonObject) -> None:
    for key in payload:
        if key.lower() in FORBIDDEN_RAW_KEYS:
            raise ToolResultDeliveryError(f"forbidden raw data key in payload: {key}")


def _validate_delivery_record_envelope(
    payload: JsonObject,
    expected_kind: str,
    allowed_keys: frozenset[str],
) -> None:
    if not isinstance(payload, dict):
        raise ToolResultDeliveryError(f"invalid payload type: {type(payload)}")
    _check_no_forbidden_keys(payload)
    if type(payload.get("schema_version")) is not int or payload.get("schema_version") != SCHEMA_VERSION:
        raise ToolResultDeliveryError(
            f"schema_version mismatch: expected {SCHEMA_VERSION}, got {payload.get('schema_version')!r}"
        )
    if payload.get("effect_kind") != EFFECT_KIND:
        raise ToolResultDeliveryError(
            f"effect_kind mismatch: expected {EFFECT_KIND!r}, got {payload.get('effect_kind')!r}"
        )
    if payload.get("record_kind") != expected_kind:
        raise ToolResultDeliveryError(
            f"record_kind mismatch: expected {expected_kind!r}, got {payload.get('record_kind')!r}"
        )
    if set(payload.keys()) != allowed_keys:
        diff = set(payload.keys()) ^ allowed_keys
        raise ToolResultDeliveryError(f"payload keys mismatch for {expected_kind}: {diff}")


@dataclass(frozen=True)
class DeliveryBatchIntent:
    batch_id: str
    session_id: str
    run_id: str
    turn: int
    items: tuple[DeliveryBatchItem, ...]
    batch_digest: str
    lane: str = ""
    operation_id: str = ""
    created_at: str = ""

    @property
    def tool_refs(self) -> tuple[str, ...]:
        return tuple(it.ref for it in self.items)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(it.tool_name for it in self.items)

    def validate(self) -> None:
        _require_bounded_str(self.batch_id, "batch_id", MAX_BATCH_ID_CHARS)
        _require_bounded_str(self.session_id, "session_id", 128)
        _require_bounded_str(self.run_id, "run_id", 128)
        _require_nonnegative_int(self.turn, "turn")
        if not self.items:
            raise ToolResultDeliveryError("items must not be empty")
        if len(self.items) > MAX_TOOL_REFS:
            raise ToolResultDeliveryError("items exceed maximum count")
        for item in self.items:
            item.validate()
        _require_bounded_str(self.batch_digest, "batch_digest", MAX_DIGEST_CHARS)
        expected_digest = compute_batch_digest(self.items)
        if self.batch_digest != expected_digest:
            raise ToolResultDeliveryError(
                f"batch_digest mismatch: expected {expected_digest!r}, got {self.batch_digest!r}"
            )

    def to_payload(self) -> JsonObject:
        self.validate()
        lane = self.lane or lane_for_run(self.run_id)
        operation_id = self.operation_id or operation_id_for_run(self.run_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_BATCH_INTENT,
            "ref": f"delivery_intent:{self.batch_id}",
            "batch_id": self.batch_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": lane,
            "operation_id": operation_id,
            "turn": self.turn,
            "items": [it.to_dict() for it in self.items],
            "batch_digest": self.batch_digest,
            "created_at": self.created_at or datetime.now(UTC).isoformat(),
        }


@dataclass(frozen=True)
class DeliveryRecoveredFact:
    batch_id: str
    session_id: str
    run_id: str
    lane: str
    operation_id: str
    recovered_effect_ids: tuple[str, ...]
    recovered_reads: int = 0
    recovered_lookups: int = 0
    created_at: str = ""


@dataclass(frozen=True)
class DeliveryBatchProjection:
    intent: DeliveryBatchIntent
    send_attempts: tuple[str, ...] = ()
    superseded_effect_ids: tuple[str, ...] = ()
    delivered_effect_ids: tuple[str, ...] = ()
    is_delivered: bool = False
    recovered_effect_ids: tuple[str, ...] = ()
    recovered_reads: int = 0
    recovered_lookups: int = 0
    is_recovered: bool = False
    abandoned_reason: str = ""
    is_abandoned: bool = False

    @property
    def is_terminal(self) -> bool:
        """Reconstruction does not acknowledge a provider delivery."""
        return (self.is_delivered or self.is_abandoned)

    @property
    def active_attempts(self) -> tuple[str, ...]:
        """Send attempts not voided by a send_superseded record."""
        voided = set(self.superseded_effect_ids)
        return tuple(eid for eid in self.send_attempts if eid not in voided)

    @property
    def is_all_safe(self) -> bool:
        if not self.intent.items:
            return False
        return all(
            it.replay_class == "safe"
            and not it.is_denied
            and is_replayable_safe_tool(it.tool_name)
            for it in self.intent.items
        )

    @property
    def can_recover_before_provider_send(self) -> bool:
        """True only if entire batch is all-safe, never terminal, no live send attempt.

        Attempts voided by send_superseded (deterministically unsent, e.g. a
        context-overflow rejection) do not block recovery. Reconstruction is
        nonterminal; acknowledged delivery and abandonment are terminals.
        """
        return (
            not self.is_delivered
            and not self.is_abandoned
            and not bool(self.active_attempts)
            and self.is_all_safe
        )


def prepare_batch_intent(
    session_id: str,
    run_id: str,
    intent: DeliveryBatchIntent,
) -> DeliveryBatchIntent:
    expected_lane = lane_for_run(run_id)
    expected_op = operation_id_for_run(run_id)
    if intent.session_id and intent.session_id != session_id:
        raise ToolResultDeliveryError(
            f"intent session_id mismatch: expected {session_id!r}, got {intent.session_id!r}"
        )
    if intent.run_id and intent.run_id != run_id:
        raise ToolResultDeliveryError(
            f"intent run_id mismatch: expected {run_id!r}, got {intent.run_id!r}"
        )
    if intent.lane and intent.lane != expected_lane:
        raise ToolResultDeliveryError(
            f"intent lane mismatch: expected {expected_lane!r}, got {intent.lane!r}"
        )
    if intent.operation_id and intent.operation_id != expected_op:
        raise ToolResultDeliveryError(
            f"intent operation_id mismatch: expected {expected_op!r}, got {intent.operation_id!r}"
        )
    return DeliveryBatchIntent(
        batch_id=intent.batch_id,
        session_id=session_id,
        run_id=run_id,
        lane=expected_lane,
        operation_id=expected_op,
        turn=intent.turn,
        items=intent.items,
        batch_digest=intent.batch_digest,
        created_at=intent.created_at,
    )


def mark_send_attempt(
    *,
    batch_id: str,
    provider_effect_id: str,
    batches: tuple[DeliveryBatchProjection, ...],
) -> DeliveryBatchProjection | None:
    clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
    clean_peid = _require_bounded_str(provider_effect_id, "provider_effect_id", MAX_EFFECT_ID_CHARS)
    projection = next((batch for batch in batches if batch.intent.batch_id == clean_batch_id), None)
    if projection is None:
        raise ToolResultDeliveryError(f"cannot record send_attempt for unknown batch: {clean_batch_id!r}")
    if projection.is_delivered or projection.is_abandoned:
        raise ToolResultDeliveryError(f"cannot record send_attempt for terminal batch: {clean_batch_id!r}")
    if clean_peid in projection.send_attempts:
        return None
    if projection.active_attempts:
        raise ToolResultDeliveryError(f"batch already has a live send_attempt: {clean_batch_id!r}")
    return replace(projection, send_attempts=(*projection.send_attempts, clean_peid))


def mark_send_superseded(
    *,
    batch_id: str,
    provider_effect_id: str,
    batches: tuple[DeliveryBatchProjection, ...],
) -> DeliveryBatchProjection | None:
    """Void a prior send_attempt that deterministically sent nothing usable.

    Returns None when already voided (idempotent). Raises when the batch is
    unknown, already delivered, or the effect id was never attempted: a void
    must name a real, live attempt, otherwise the ledger could hide a send.
    """
    clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
    clean_peid = _require_bounded_str(provider_effect_id, "provider_effect_id", MAX_EFFECT_ID_CHARS)
    projection = next((batch for batch in batches if batch.intent.batch_id == clean_batch_id), None)
    if projection is None:
        raise ToolResultDeliveryError(f"cannot record send_superseded for unknown batch: {clean_batch_id!r}")
    if projection.is_delivered or projection.is_abandoned:
        raise ToolResultDeliveryError(f"cannot record send_superseded for terminal batch: {clean_batch_id!r}")
    if clean_peid in projection.superseded_effect_ids:
        return None
    if clean_peid not in projection.send_attempts:
        raise ToolResultDeliveryError(
            f"cannot record send_superseded without matching send_attempt: {clean_batch_id!r}"
        )
    return replace(projection, superseded_effect_ids=(*projection.superseded_effect_ids, clean_peid))


def mark_delivered(
    *,
    batch_id: str,
    provider_effect_id: str,
    batches: tuple[DeliveryBatchProjection, ...],
) -> DeliveryBatchProjection | None:
    clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
    clean_peid = _require_bounded_str(provider_effect_id, "provider_effect_id", MAX_EFFECT_ID_CHARS)
    projection = next((batch for batch in batches if batch.intent.batch_id == clean_batch_id), None)
    if projection is None:
        raise ToolResultDeliveryError(f"cannot record delivered for unknown batch: {clean_batch_id!r}")
    if projection.is_abandoned:
        raise ToolResultDeliveryError(f"cannot record delivered for abandoned batch: {clean_batch_id!r}")
    if clean_peid in projection.delivered_effect_ids:
        return None
    if projection.delivered_effect_ids:
        raise ToolResultDeliveryError(f"batch already has a delivered receipt: {clean_batch_id!r}")
    if clean_peid not in projection.active_attempts:
        raise ToolResultDeliveryError(
            f"cannot record delivered without matching live send_attempt: {clean_batch_id!r}"
        )
    return replace(projection, delivered_effect_ids=(clean_peid,), is_delivered=True)


def mark_recovered(
    *,
    batch_id: str,
    recovered_effect_ids: Sequence[str],
    recovered_reads: int = 0,
    recovered_lookups: int = 0,
    batches: tuple[DeliveryBatchProjection, ...],
) -> DeliveryBatchProjection | None:
    clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
    if not isinstance(recovered_effect_ids, (list, tuple)):
        raise ToolResultDeliveryError("recovered_effect_ids must be a sequence")
    clean_effect_ids = [
        _require_bounded_str(eid, "recovered_effect_id", MAX_EFFECT_ID_CHARS)
        for eid in recovered_effect_ids
    ]
    rec_reads = _require_nonnegative_int(recovered_reads, "recovered_reads")
    rec_lookups = _require_nonnegative_int(recovered_lookups, "recovered_lookups")
    projection = next((batch for batch in batches if batch.intent.batch_id == clean_batch_id), None)
    if projection is None:
        raise ToolResultDeliveryError(f"cannot record recovered for unknown batch: {clean_batch_id!r}")
    if projection.is_abandoned:
        raise ToolResultDeliveryError(f"cannot record recovered for abandoned batch: {clean_batch_id!r}")
    if projection.is_recovered:
        if (
            list(projection.recovered_effect_ids) == clean_effect_ids
            and projection.recovered_reads == rec_reads
            and projection.recovered_lookups == rec_lookups
        ):
            return None
        raise ToolResultDeliveryError(f"conflicting recovered record for batch {clean_batch_id!r}")
    return replace(projection, is_recovered=True, recovered_effect_ids=tuple(clean_effect_ids),
                   recovered_reads=rec_reads, recovered_lookups=rec_lookups)


def mark_abandoned(
    *,
    batch_id: str,
    reason: str,
    batches: tuple[DeliveryBatchProjection, ...],
) -> DeliveryBatchProjection | None:
    """Close an undelivered batch when writer/repair settles past it.

    Reconstruction may precede abandonment, but acknowledged delivery may
    not. Idempotent on same reason, conflicting on different reason.
    """
    clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
    clean_reason = _require_bounded_str(reason, "reason", 80)
    if clean_reason not in ABANDONED_REASONS:
        raise ToolResultDeliveryError(f"invalid abandoned reason: {clean_reason!r}")
    projection = next((batch for batch in batches if batch.intent.batch_id == clean_batch_id), None)
    if projection is None:
        raise ToolResultDeliveryError(f"cannot record abandoned for unknown batch: {clean_batch_id!r}")
    if projection.is_delivered:
        raise ToolResultDeliveryError(f"cannot record abandoned for terminal batch: {clean_batch_id!r}")
    if projection.is_abandoned:
        if projection.abandoned_reason == clean_reason:
            return None
        raise ToolResultDeliveryError(f"conflicting abandoned record for batch {clean_batch_id!r}")
    return replace(projection, is_abandoned=True, abandoned_reason=clean_reason)


def _batch_intent_from_payload(payload: JsonObject, batch_id: str) -> DeliveryBatchIntent:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ToolResultDeliveryError(
            f"batch_intent missing or invalid items in batch {batch_id!r}"
        )
    items = tuple(DeliveryBatchItem.from_dict(decode_json_object(it)) for it in raw_items)
    turn = _require_nonnegative_int(payload.get("turn"), "turn")
    intent = DeliveryBatchIntent(
        batch_id=batch_id,
        session_id=str(payload.get("session_id") or ""),
        run_id=str(payload.get("run_id") or ""),
        lane=str(payload.get("lane") or ""),
        operation_id=str(payload.get("operation_id") or ""),
        turn=turn,
        items=items,
        batch_digest=str(payload.get("batch_digest") or ""),
        created_at=str(payload.get("created_at") or ""),
    )
    intent.validate()
    return intent


def _check_delivery_links(
    *,
    send_attempts: dict[str, list[str]],
    superseded: dict[str, list[str]],
    delivered: dict[str, list[str]],
) -> None:
    for bid, voided in superseded.items():
        attempted = set(send_attempts.get(bid, ()))
        unknown = [peid for peid in voided if peid not in attempted]
        if unknown:
            raise ToolResultDeliveryError(
                f"send_superseded records without matching send_attempt: {bid!r}: {unknown}"
            )
    conflicting_attempts = {
        bid: ids
        for bid, ids in send_attempts.items()
        if len([peid for peid in ids if peid not in set(superseded.get(bid, ()))]) > 1
    }
    if conflicting_attempts:
        raise ToolResultDeliveryError(
            f"batch has conflicting live send_attempt records: {conflicting_attempts}"
        )
    conflicting_delivered = {
        bid: ids
        for bid, ids in delivered.items()
        if len(ids) != 1 or len(set(ids)) != 1
    }
    if conflicting_delivered:
        raise ToolResultDeliveryError(
            f"batch has conflicting delivered records: {conflicting_delivered}"
        )
    delivered_without_attempt: dict[str, list[str]] = {}
    for bid, delivered_ids in delivered.items():
        attempted = set(send_attempts.get(bid, ()))
        missing = [peid for peid in delivered_ids if peid not in attempted]
        if missing:
            delivered_without_attempt[bid] = missing
    if delivered_without_attempt:
        raise ToolResultDeliveryError(
            f"delivered records without matching send_attempt: {delivered_without_attempt}"
        )


def delivery_batches_from_snapshot(value: object, *, session_id: str,
                                   run_id: str) -> tuple[DeliveryBatchProjection, ...]:
    if not isinstance(value, list):
        raise ToolResultDeliveryError("invalid delivery checkpoint")
    snapshots: list[object] = value
    batches: dict[str, DeliveryBatchProjection] = {}
    for snapshot in snapshots:
        batch = _batch_from_snapshot(decode_json_object(snapshot), session_id, run_id)
        if batch.intent.batch_id in batches:
            raise ToolResultDeliveryError("duplicate delivery batch in checkpoint")
        batches[batch.intent.batch_id] = batch
    return tuple(batches.values())


def _batch_from_snapshot(snapshot: JsonObject, session_id: str, run_id: str) -> DeliveryBatchProjection:
    payload = decode_json_object(snapshot["intent"])
    _validate_delivery_record_envelope(payload, RECORD_KIND_BATCH_INTENT, _INTENT_PAYLOAD_KEYS)
    batch_id = _require_bounded_str(payload["batch_id"], "batch_id", MAX_BATCH_ID_CHARS)
    intent = _batch_intent_from_payload(payload, batch_id)
    if (intent.session_id != session_id or intent.run_id != run_id
            or intent.lane != lane_for_run(run_id) or intent.operation_id != operation_id_for_run(run_id)):
        raise ToolResultDeliveryError("delivery checkpoint boundary mismatch")
    attempts, superseded, delivered = (_snapshot_effect_ids(snapshot[field], field)
        for field in ("send_attempts", "superseded_effect_ids", "delivered_effect_ids"))
    _check_delivery_links(send_attempts={batch_id: list(attempts)}, superseded={batch_id: list(superseded)},
                          delivered={batch_id: list(delivered)} if delivered else {})
    is_recovered = require_json_boolean(snapshot["is_recovered"], "is_recovered")
    is_abandoned = require_json_boolean(snapshot["is_abandoned"], "is_abandoned")
    reason = require_json_string(snapshot["abandoned_reason"], "abandoned_reason")
    if is_abandoned and (delivered or reason not in ABANDONED_REASONS):
        raise ToolResultDeliveryError("invalid abandoned delivery checkpoint")
    return DeliveryBatchProjection(
        intent, attempts, superseded, delivered, bool(delivered),
        _snapshot_effect_ids(snapshot["recovered_effect_ids"], "recovered_effect_ids"),
        _require_nonnegative_int(snapshot["recovered_reads"], "recovered_reads"),
        _require_nonnegative_int(snapshot["recovered_lookups"], "recovered_lookups"),
        is_recovered, reason, is_abandoned,
    )


def _snapshot_effect_ids(value: JsonValue, field: str) -> tuple[str, ...]:
    return tuple(_require_bounded_str(identity, field, MAX_EFFECT_ID_CHARS)
                 for identity in require_json_strings(value, field))


def delivery_from_checkpoint(task: TaskCheckpoint, *, session_id: str) -> tuple[DeliveryBatchProjection, ...]:
    if task.conversation_id != session_id:
        raise RuntimeOperationTransitionError("checkpoint conversation boundary mismatch")
    return delivery_batches_from_snapshot(thaw_json(task.state.get("delivery_batches", ())),
                                          session_id=session_id, run_id=task.task_id)


class ToolResultDeliveryStore:
    """Read the delivery batches of the durable task."""

    def __init__(self, *, durable: DurableSession) -> None:
        self.durable = durable

    def load_batches(self, session_id: str, run_id: str) -> tuple[DeliveryBatchProjection, ...]:
        try:
            task = self.durable.task(run_id)
        except KeyError:
            return ()
        return delivery_from_checkpoint(task, session_id=session_id)

    def load_recovered_facts(self, session_id: str, run_id: str) -> tuple[DeliveryRecoveredFact, ...]:
        return tuple(
            DeliveryRecoveredFact(batch.intent.batch_id, session_id, run_id,
                batch.intent.lane, batch.intent.operation_id, batch.recovered_effect_ids,
                batch.recovered_reads, batch.recovered_lookups, batch.intent.created_at)
            for batch in self.load_batches(session_id, run_id) if batch.is_recovered
        )

    def undelivered_replayable_batches(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[DeliveryBatchProjection, ...]:
        """Return undelivered batches where every tool call is replayable safe and zero send attempts."""
        batches = self.load_batches(session_id, run_id)
        return tuple(b for b in batches if b.can_recover_before_provider_send)


__all__ = [
    "ABANDONED_REASONS",
    "EFFECT_KIND",
    "RECORD_KIND_BATCH_INTENT",
    "DeliveryBatchItem",
    "DeliveryBatchIntent",
    "DeliveryBatchProjection",
    "DeliveryCommit",
    "DeliveryRecoveredFact",
    "ToolResultDeliveryError",
    "ToolResultDeliveryStore",
    "compute_batch_digest",
    "new_batch_id",
    "prepare_batch_intent",
]
