"""Runtime tool result delivery tracking, receipts, and projection.

The session log is the durable fact source. This module records bounded receipts
around the delivery of tool results to model providers:
1. batch_intent: recorded before tool execution in a turn, capturing all planned results
2. send_attempt: recorded when a provider send effect starts
3. delivered: recorded when the provider request settles successfully
4. recovered: recorded when safe results are reconstructed on resume

Strictly bounded: never records raw result text, prompts, replies, stdout,
stderr, diffs, or source bodies.

Does not import agents, provider, ghost, operations, or tool runners.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any
import uuid

from codey.runtime.effects import lane_for_run, operation_id_for_run
from codey.runtime.replay_policy import is_replayable_safe_tool
from codey.runtime.session_log import RuntimeLogEntry, RuntimeSessionLog

SCHEMA_VERSION = 1
EFFECT_KIND = "tool_result_delivery"

RECORD_KIND_BATCH_INTENT = "batch_intent"
RECORD_KIND_SEND_ATTEMPT = "send_attempt"
RECORD_KIND_DELIVERED = "delivered"
RECORD_KIND_RECOVERED = "recovered"
RECORD_KINDS = frozenset({
    RECORD_KIND_BATCH_INTENT,
    RECORD_KIND_SEND_ATTEMPT,
    RECORD_KIND_DELIVERED,
    RECORD_KIND_RECOVERED,
})

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

_SEND_ATTEMPT_PAYLOAD_KEYS = frozenset({
    "schema_version",
    "effect_kind",
    "record_kind",
    "ref",
    "batch_id",
    "session_id",
    "run_id",
    "lane",
    "operation_id",
    "provider_effect_id",
    "created_at",
})

_DELIVERED_PAYLOAD_KEYS = frozenset({
    "schema_version",
    "effect_kind",
    "record_kind",
    "ref",
    "batch_id",
    "session_id",
    "run_id",
    "lane",
    "operation_id",
    "provider_effect_id",
    "created_at",
})

_RECOVERED_PAYLOAD_KEYS = frozenset({
    "schema_version",
    "effect_kind",
    "record_kind",
    "ref",
    "batch_id",
    "session_id",
    "run_id",
    "lane",
    "operation_id",
    "recovered_effect_ids",
    "recovered_reads",
    "recovered_lookups",
    "created_at",
})


class ToolResultDeliveryError(ValueError):
    """Raised when delivery receipt data violates schema, bounds, or invariants."""


def _require_nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ToolResultDeliveryError(f"{name} must be a non-negative int, got {value!r}")
    return value


def _require_bounded_str(value: Any, name: str, max_chars: int) -> str:
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

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "tool_index": self.tool_index,
            "tool_name": self.tool_name,
            "ref": self.ref,
            "replay_class": self.replay_class,
            "is_denied": self.is_denied,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeliveryBatchItem:
        if not isinstance(data, Mapping):
            raise ToolResultDeliveryError(f"invalid item payload type: {type(data)}")
        if set(data.keys()) != _ITEM_PAYLOAD_KEYS:
            diff = set(data.keys()) ^ _ITEM_PAYLOAD_KEYS
            raise ToolResultDeliveryError(f"item payload key mismatch: {diff}")
        item = cls(
            tool_index=data["tool_index"],
            tool_name=data["tool_name"],
            ref=data["ref"],
            replay_class=data["replay_class"],
            is_denied=data["is_denied"],
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
    clean_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_")[:40]
    token = uuid.uuid4().hex[:12]
    return f"deliv-{clean_run}-t{turn}-{token}"


def _check_no_forbidden_keys(payload: dict[str, Any]) -> None:
    for key in payload:
        if key.lower() in FORBIDDEN_RAW_KEYS:
            raise ToolResultDeliveryError(f"forbidden raw data key in payload: {key}")


def _validate_delivery_record_envelope(
    payload: dict[str, Any],
    expected_kind: str,
    allowed_keys: frozenset[str],
) -> None:
    if not isinstance(payload, dict):
        raise ToolResultDeliveryError(f"invalid payload type: {type(payload)}")
    _check_no_forbidden_keys(payload)
    if payload.get("schema_version") != SCHEMA_VERSION:
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


def _validate_run_boundary(
    entry: RuntimeLogEntry,
    payload: dict[str, Any],
    session_id: str,
    run_id: str,
) -> bool:
    """Strictly align with RuntimeEffectStore run boundary rules."""
    expected_lane = lane_for_run(run_id)
    expected_op = operation_id_for_run(run_id)

    entry_lane_match = (entry.lane == expected_lane)
    entry_op_match = (entry.operation_id == expected_op)
    payload_run_match = (payload.get("run_id") == run_id)
    payload_op_match = (payload.get("operation_id") == expected_op)
    payload_lane_match = (payload.get("lane") == expected_lane)

    # Check if this record is related to the queried run
    matches_any = (
        entry_lane_match
        or entry_op_match
        or payload_run_match
        or payload_op_match
        or payload_lane_match
    )
    if not matches_any:
        return False

    # If it is related, all 4 dimensions MUST match strictly
    if (
        entry.lane != expected_lane
        or entry.operation_id != expected_op
        or payload.get("session_id") != session_id
        or payload.get("run_id") != run_id
        or payload.get("lane") != expected_lane
        or payload.get("operation_id") != expected_op
    ):
        raise ToolResultDeliveryError(
            f"delivery record run boundary mismatch: "
            f"entry=({entry.lane!r}, {entry.operation_id!r}), "
            f"payload=({payload.get('session_id')!r}, {payload.get('run_id')!r}, "
            f"{payload.get('lane')!r}, {payload.get('operation_id')!r}), "
            f"expected=({session_id!r}, {run_id!r}, {expected_lane!r}, {expected_op!r})"
        )
    return True


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

    def to_payload(self) -> dict[str, Any]:
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
            "turn": int(self.turn),
            "items": [it.to_dict() for it in self.items],
            "batch_digest": self.batch_digest,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
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
    delivered_effect_ids: tuple[str, ...] = ()
    is_delivered: bool = False
    recovered_effect_ids: tuple[str, ...] = ()
    recovered_reads: int = 0
    recovered_lookups: int = 0
    is_recovered: bool = False

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
        """True only if entire batch is all-safe, never delivered, and zero send attempts."""
        return (
            not self.is_delivered
            and not bool(self.send_attempts)
            and self.is_all_safe
        )


class ToolResultDeliveryStore:
    """Appends delivery receipts to session log and projects batch states."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self._session_log = session_log

    @property
    def session_log(self) -> RuntimeSessionLog:
        return self._session_log

    def _projection_for_batch(
        self,
        session_id: str,
        run_id: str,
        batch_id: str,
    ) -> DeliveryBatchProjection | None:
        for batch in self.load_batches(session_id, run_id):
            if batch.intent.batch_id == batch_id:
                return batch
        return None

    def _append_delivery_record(
        self,
        session_id: str,
        lane: str,
        operation_id: str,
        payload: dict[str, Any],
        allowed_keys: frozenset[str],
    ) -> None:
        _check_no_forbidden_keys(payload)
        if not allowed_keys.issuperset(payload.keys()):
            extra = set(payload.keys()) - allowed_keys
            raise ToolResultDeliveryError(f"unknown payload fields: {extra}")
        self._session_log.append(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            kind="operation_effect",
            payload=payload,
        )

    def record_batch_intent(
        self,
        session_id: str,
        run_id: str,
        intent: DeliveryBatchIntent,
    ) -> None:
        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)

        # Reject mismatched coordinates strictly
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

        complete_intent = DeliveryBatchIntent(
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
        self._append_delivery_record(
            session_id=session_id,
            lane=expected_lane,
            operation_id=expected_op,
            payload=complete_intent.to_payload(),
            allowed_keys=_INTENT_PAYLOAD_KEYS,
        )

    def record_send_attempt(
        self,
        session_id: str,
        run_id: str,
        *,
        batch_id: str,
        provider_effect_id: str,
        lane: str = "",
        operation_id: str = "",
    ) -> None:
        clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
        clean_peid = _require_bounded_str(provider_effect_id, "provider_effect_id", MAX_EFFECT_ID_CHARS)

        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)
        if lane and lane != expected_lane:
            raise ToolResultDeliveryError(f"lane mismatch: expected {expected_lane!r}, got {lane!r}")
        if operation_id and operation_id != expected_op:
            raise ToolResultDeliveryError(f"operation_id mismatch: expected {expected_op!r}, got {operation_id!r}")

        projection = self._projection_for_batch(session_id, run_id, clean_batch_id)
        if projection is None:
            raise ToolResultDeliveryError(f"cannot record send_attempt for unknown batch: {clean_batch_id!r}")
        if projection.is_delivered:
            raise ToolResultDeliveryError(f"cannot record send_attempt for delivered batch: {clean_batch_id!r}")
        if clean_peid in projection.send_attempts:
            return
        if projection.send_attempts:
            raise ToolResultDeliveryError(f"batch already has a send_attempt: {clean_batch_id!r}")

        payload = {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_SEND_ATTEMPT,
            "ref": f"delivery_attempt:{clean_batch_id}:{clean_peid}",
            "batch_id": clean_batch_id,
            "session_id": session_id,
            "run_id": run_id,
            "lane": expected_lane,
            "operation_id": expected_op,
            "provider_effect_id": clean_peid,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_delivery_record(
            session_id=session_id,
            lane=expected_lane,
            operation_id=expected_op,
            payload=payload,
            allowed_keys=_SEND_ATTEMPT_PAYLOAD_KEYS,
        )

    def record_delivered(
        self,
        session_id: str,
        run_id: str,
        *,
        batch_id: str,
        provider_effect_id: str,
        lane: str = "",
        operation_id: str = "",
    ) -> None:
        clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
        clean_peid = _require_bounded_str(provider_effect_id, "provider_effect_id", MAX_EFFECT_ID_CHARS)

        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)
        if lane and lane != expected_lane:
            raise ToolResultDeliveryError(f"lane mismatch: expected {expected_lane!r}, got {lane!r}")
        if operation_id and operation_id != expected_op:
            raise ToolResultDeliveryError(f"operation_id mismatch: expected {expected_op!r}, got {operation_id!r}")

        projection = self._projection_for_batch(session_id, run_id, clean_batch_id)
        if projection is None:
            raise ToolResultDeliveryError(f"cannot record delivered for unknown batch: {clean_batch_id!r}")
        if clean_peid in projection.delivered_effect_ids:
            return
        if projection.delivered_effect_ids:
            raise ToolResultDeliveryError(f"batch already has a delivered receipt: {clean_batch_id!r}")
        if clean_peid not in projection.send_attempts:
            raise ToolResultDeliveryError(
                f"cannot record delivered without matching send_attempt: {clean_batch_id!r}"
            )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_DELIVERED,
            "ref": f"delivery_delivered:{clean_batch_id}:{clean_peid}",
            "batch_id": clean_batch_id,
            "session_id": session_id,
            "run_id": run_id,
            "lane": expected_lane,
            "operation_id": expected_op,
            "provider_effect_id": clean_peid,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_delivery_record(
            session_id=session_id,
            lane=expected_lane,
            operation_id=expected_op,
            payload=payload,
            allowed_keys=_DELIVERED_PAYLOAD_KEYS,
        )

    def record_recovered(
        self,
        session_id: str,
        run_id: str,
        *,
        batch_id: str,
        recovered_effect_ids: Sequence[str],
        recovered_reads: int = 0,
        recovered_lookups: int = 0,
        lane: str = "",
        operation_id: str = "",
    ) -> None:
        clean_batch_id = _require_bounded_str(batch_id, "batch_id", MAX_BATCH_ID_CHARS)
        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)
        if lane and lane != expected_lane:
            raise ToolResultDeliveryError(f"lane mismatch: expected {expected_lane!r}, got {lane!r}")
        if operation_id and operation_id != expected_op:
            raise ToolResultDeliveryError(f"operation_id mismatch: expected {expected_op!r}, got {operation_id!r}")

        if not isinstance(recovered_effect_ids, (list, tuple)):
            raise ToolResultDeliveryError("recovered_effect_ids must be a sequence")
        clean_effect_ids = [
            _require_bounded_str(eid, "recovered_effect_id", MAX_EFFECT_ID_CHARS)
            for eid in recovered_effect_ids
        ]
        rec_reads = _require_nonnegative_int(recovered_reads, "recovered_reads")
        rec_lookups = _require_nonnegative_int(recovered_lookups, "recovered_lookups")

        # Check existing recovered records for this batch in current operation for idempotency
        for rkind, payload in self._iter_validated_delivery_records(session_id, run_id):
            if (
                rkind == RECORD_KIND_RECOVERED
                and payload.get("batch_id") == clean_batch_id
            ):
                existing_eids = payload.get("recovered_effect_ids")
                existing_reads = payload.get("recovered_reads")
                existing_lookups = payload.get("recovered_lookups")
                if (
                    existing_eids == clean_effect_ids
                    and existing_reads == rec_reads
                    and existing_lookups == rec_lookups
                ):
                    return  # Idempotent no-op
                raise ToolResultDeliveryError(
                    f"conflicting recovered record for batch {clean_batch_id!r}"
                )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_RECOVERED,
            "ref": f"delivery_recovered:{clean_batch_id}",
            "batch_id": clean_batch_id,
            "session_id": session_id,
            "run_id": run_id,
            "lane": expected_lane,
            "operation_id": expected_op,
            "recovered_effect_ids": clean_effect_ids,
            "recovered_reads": rec_reads,
            "recovered_lookups": rec_lookups,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_delivery_record(
            session_id=session_id,
            lane=expected_lane,
            operation_id=expected_op,
            payload=payload,
            allowed_keys=_RECOVERED_PAYLOAD_KEYS,
        )

    def _iter_validated_delivery_records(
        self,
        session_id: str,
        run_id: str,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Iterate and strictly validate all delivery records for this run."""
        entries = self._session_log.read(session_id)
        for entry in entries:
            if entry.kind != "operation_effect":
                continue
            payload = entry.payload
            if not isinstance(payload, dict):
                continue
            if payload.get("effect_kind") != EFFECT_KIND:
                continue

            # Validate run boundaries strictly
            if not _validate_run_boundary(entry, payload, session_id, run_id):
                continue

            rkind = payload.get("record_kind")
            if rkind not in RECORD_KINDS:
                raise ToolResultDeliveryError(f"unknown delivery record_kind: {rkind!r}")

            batch_id = payload.get("batch_id")
            _require_bounded_str(batch_id, "batch_id in delivery record", MAX_BATCH_ID_CHARS)

            if rkind == RECORD_KIND_BATCH_INTENT:
                _validate_delivery_record_envelope(payload, RECORD_KIND_BATCH_INTENT, _INTENT_PAYLOAD_KEYS)
            elif rkind == RECORD_KIND_SEND_ATTEMPT:
                _validate_delivery_record_envelope(payload, RECORD_KIND_SEND_ATTEMPT, _SEND_ATTEMPT_PAYLOAD_KEYS)
                peid = payload.get("provider_effect_id")
                _require_bounded_str(peid, "provider_effect_id in send_attempt", MAX_EFFECT_ID_CHARS)
            elif rkind == RECORD_KIND_DELIVERED:
                _validate_delivery_record_envelope(payload, RECORD_KIND_DELIVERED, _DELIVERED_PAYLOAD_KEYS)
                peid = payload.get("provider_effect_id")
                _require_bounded_str(peid, "provider_effect_id in delivered", MAX_EFFECT_ID_CHARS)
            elif rkind == RECORD_KIND_RECOVERED:
                _validate_delivery_record_envelope(payload, RECORD_KIND_RECOVERED, _RECOVERED_PAYLOAD_KEYS)
                _require_nonnegative_int(payload.get("recovered_reads"), "recovered_reads")
                _require_nonnegative_int(payload.get("recovered_lookups"), "recovered_lookups")
                effect_ids = payload.get("recovered_effect_ids")
                if not isinstance(effect_ids, list):
                    raise ToolResultDeliveryError("recovered_effect_ids must be a list")
                for eid in effect_ids:
                    _require_bounded_str(eid, "recovered_effect_id", MAX_EFFECT_ID_CHARS)

            yield rkind, payload

    def load_batches(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[DeliveryBatchProjection, ...]:
        intents: dict[str, DeliveryBatchIntent] = {}
        ordered_batch_ids: list[str] = []
        send_attempts: dict[str, list[str]] = {}
        delivered: dict[str, list[str]] = {}
        recovered_facts: dict[str, dict[str, Any]] = {}

        for rkind, payload in self._iter_validated_delivery_records(session_id, run_id):
            batch_id = payload["batch_id"]

            if rkind == RECORD_KIND_BATCH_INTENT:
                raw_items = payload.get("items")
                if not isinstance(raw_items, list) or not raw_items:
                    raise ToolResultDeliveryError(
                        f"batch_intent missing or invalid items in batch {batch_id!r}"
                    )
                items = tuple(DeliveryBatchItem.from_dict(it) for it in raw_items)
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

                if batch_id in intents:
                    existing = intents[batch_id]
                    if (
                        existing.turn != intent.turn
                        or existing.items != intent.items
                        or existing.batch_digest != intent.batch_digest
                    ):
                        raise ToolResultDeliveryError(
                            f"conflicting duplicate batch intent for batch_id {batch_id!r}"
                        )
                else:
                    intents[batch_id] = intent
                    ordered_batch_ids.append(batch_id)

            elif rkind == RECORD_KIND_SEND_ATTEMPT:
                peid = payload["provider_effect_id"]
                send_attempts.setdefault(batch_id, []).append(peid)

            elif rkind == RECORD_KIND_DELIVERED:
                peid = payload["provider_effect_id"]
                delivered.setdefault(batch_id, []).append(peid)

            elif rkind == RECORD_KIND_RECOVERED:
                if batch_id in recovered_facts:
                    existing = recovered_facts[batch_id]
                    if (
                        list(existing.get("recovered_effect_ids", [])) != list(payload.get("recovered_effect_ids", []))
                        or existing.get("recovered_reads") != payload.get("recovered_reads")
                        or existing.get("recovered_lookups") != payload.get("recovered_lookups")
                    ):
                        raise ToolResultDeliveryError(f"conflicting recovered facts for batch {batch_id!r}")
                else:
                    recovered_facts[batch_id] = payload

        # Strictly reject orphan send_attempt and delivered records
        known_intent_ids = set(intents.keys())
        orphan_attempts = set(send_attempts.keys()) - known_intent_ids
        if orphan_attempts:
            raise ToolResultDeliveryError(
                f"orphan send_attempt records without corresponding batch_intent: {orphan_attempts}"
            )
        orphan_delivered = set(delivered.keys()) - known_intent_ids
        if orphan_delivered:
            raise ToolResultDeliveryError(
                f"orphan delivered records without corresponding batch_intent: {orphan_delivered}"
            )
        conflicting_attempts = {
            bid: ids
            for bid, ids in send_attempts.items()
            if len(ids) != 1 or len(set(ids)) != 1
        }
        if conflicting_attempts:
            raise ToolResultDeliveryError(
                f"batch has conflicting send_attempt records: {conflicting_attempts}"
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

        projections: list[DeliveryBatchProjection] = []
        for bid in ordered_batch_ids:
            intent = intents[bid]
            attempts = tuple(send_attempts.get(bid, ()))
            deliv_ids = tuple(delivered.get(bid, ()))
            rec = recovered_facts.get(bid)

            is_deliv = bool(deliv_ids)
            rec_ids = tuple(rec.get("recovered_effect_ids", ())) if rec else ()
            rec_reads = int(rec.get("recovered_reads", 0)) if rec else 0
            rec_lookups = int(rec.get("recovered_lookups", 0)) if rec else 0
            is_rec = rec is not None

            projections.append(
                DeliveryBatchProjection(
                    intent=intent,
                    send_attempts=attempts,
                    delivered_effect_ids=deliv_ids,
                    is_delivered=is_deliv,
                    recovered_effect_ids=rec_ids,
                    recovered_reads=rec_reads,
                    recovered_lookups=rec_lookups,
                    is_recovered=is_rec,
                )
            )

        return tuple(projections)

    def load_recovered_facts(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[DeliveryRecoveredFact, ...]:
        """Load durable recovered facts directly from session log, coalescing by batch_id."""
        facts_by_batch: dict[str, DeliveryRecoveredFact] = {}
        for rkind, payload in self._iter_validated_delivery_records(session_id, run_id):
            if rkind != RECORD_KIND_RECOVERED:
                continue
            batch_id = payload["batch_id"]
            rec_reads = payload["recovered_reads"]
            rec_lookups = payload["recovered_lookups"]
            effect_ids = payload["recovered_effect_ids"]

            fact = DeliveryRecoveredFact(
                batch_id=batch_id,
                session_id=str(payload.get("session_id") or ""),
                run_id=str(payload.get("run_id") or ""),
                lane=str(payload.get("lane") or ""),
                operation_id=str(payload.get("operation_id") or ""),
                recovered_effect_ids=tuple(str(e) for e in effect_ids),
                recovered_reads=rec_reads,
                recovered_lookups=rec_lookups,
                created_at=str(payload.get("created_at") or ""),
            )
            if batch_id in facts_by_batch:
                existing = facts_by_batch[batch_id]
                if (
                    existing.recovered_effect_ids != fact.recovered_effect_ids
                    or existing.recovered_reads != fact.recovered_reads
                    or existing.recovered_lookups != fact.recovered_lookups
                ):
                    raise ToolResultDeliveryError(f"conflicting recovered facts for batch {batch_id!r}")
            else:
                facts_by_batch[batch_id] = fact
        return tuple(facts_by_batch.values())

    def undelivered_replayable_batches(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[DeliveryBatchProjection, ...]:
        """Return undelivered batches where every tool call is replayable safe and zero send attempts."""
        batches = self.load_batches(session_id, run_id)
        return tuple(b for b in batches if b.can_recover_before_provider_send)


__all__ = [
    "EFFECT_KIND",
    "RECORD_KIND_BATCH_INTENT",
    "RECORD_KIND_DELIVERED",
    "RECORD_KIND_RECOVERED",
    "RECORD_KIND_SEND_ATTEMPT",
    "RECORD_KINDS",
    "DeliveryBatchItem",
    "DeliveryBatchIntent",
    "DeliveryBatchProjection",
    "DeliveryRecoveredFact",
    "ToolResultDeliveryError",
    "ToolResultDeliveryStore",
    "compute_batch_digest",
    "new_batch_id",
]
