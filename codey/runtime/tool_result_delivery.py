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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any
import uuid

from codey.runtime.effects import lane_for_run, operation_id_for_run
from codey.runtime.replay_policy import is_replayable_safe_tool
from codey.runtime.session_log import RuntimeSessionLog

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
    "tool_refs",
    "tool_names",
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


@dataclass(frozen=True)
class DeliveryBatchItem:
    tool_index: int
    tool_name: str
    ref: str
    replay_class: str
    is_denied: bool = False

    def validate(self) -> None:
        if self.tool_index < 0:
            raise ToolResultDeliveryError(f"invalid tool_index: {self.tool_index}")
        if not self.tool_name or not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ToolResultDeliveryError(f"invalid tool_name: {self.tool_name!r}")
        if not self.ref or not isinstance(self.ref, str) or len(self.ref) > MAX_REF_CHARS:
            raise ToolResultDeliveryError(f"invalid ref: {self.ref!r}")
        if self.replay_class not in {"safe", "unsafe"}:
            raise ToolResultDeliveryError(f"invalid replay_class: {self.replay_class!r}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "tool_index": int(self.tool_index),
            "tool_name": str(self.tool_name),
            "ref": str(self.ref),
            "replay_class": str(self.replay_class),
            "is_denied": bool(self.is_denied),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeliveryBatchItem:
        if not isinstance(data, Mapping):
            raise ToolResultDeliveryError(f"invalid item payload: {data!r}")
        item = cls(
            tool_index=int(data.get("tool_index", 0)),
            tool_name=str(data.get("tool_name") or ""),
            ref=str(data.get("ref") or ""),
            replay_class=str(data.get("replay_class") or "unsafe"),
            is_denied=bool(data.get("is_denied", False)),
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
        if not self.batch_id or len(self.batch_id) > MAX_BATCH_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_id: {self.batch_id!r}")
        if not self.session_id:
            raise ToolResultDeliveryError("session_id must not be empty")
        if not self.run_id:
            raise ToolResultDeliveryError("run_id must not be empty")
        if self.turn < 0:
            raise ToolResultDeliveryError(f"turn must be non-negative: {self.turn}")
        if not self.items:
            raise ToolResultDeliveryError("items must not be empty")
        if len(self.items) > MAX_TOOL_REFS:
            raise ToolResultDeliveryError("items exceed maximum count")
        for item in self.items:
            item.validate()
        if not self.batch_digest or len(self.batch_digest) > MAX_DIGEST_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_digest: {self.batch_digest!r}")
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
            "tool_refs": list(self.tool_refs),
            "tool_names": list(self.tool_names),
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
        lane = intent.lane or lane_for_run(run_id)
        operation_id = intent.operation_id or operation_id_for_run(run_id)
        if intent.lane != lane or intent.operation_id != operation_id:
            intent = DeliveryBatchIntent(
                batch_id=intent.batch_id,
                session_id=intent.session_id or session_id,
                run_id=intent.run_id or run_id,
                lane=lane,
                operation_id=operation_id,
                turn=intent.turn,
                items=intent.items,
                batch_digest=intent.batch_digest,
                created_at=intent.created_at,
            )
        self._append_delivery_record(
            session_id=session_id,
            lane=intent.lane,
            operation_id=intent.operation_id,
            payload=intent.to_payload(),
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
        if not batch_id or len(batch_id) > MAX_BATCH_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_id: {batch_id!r}")
        if not provider_effect_id or len(provider_effect_id) > MAX_EFFECT_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid provider_effect_id: {provider_effect_id!r}")
        lane = lane or lane_for_run(run_id)
        operation_id = operation_id or operation_id_for_run(run_id)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_SEND_ATTEMPT,
            "ref": f"delivery_attempt:{batch_id}:{provider_effect_id}",
            "batch_id": batch_id,
            "session_id": session_id,
            "run_id": run_id,
            "lane": lane,
            "operation_id": operation_id,
            "provider_effect_id": provider_effect_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_delivery_record(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
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
        if not batch_id or len(batch_id) > MAX_BATCH_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_id: {batch_id!r}")
        if not provider_effect_id or len(provider_effect_id) > MAX_EFFECT_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid provider_effect_id: {provider_effect_id!r}")
        lane = lane or lane_for_run(run_id)
        operation_id = operation_id or operation_id_for_run(run_id)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_DELIVERED,
            "ref": f"delivery_delivered:{batch_id}:{provider_effect_id}",
            "batch_id": batch_id,
            "session_id": session_id,
            "run_id": run_id,
            "lane": lane,
            "operation_id": operation_id,
            "provider_effect_id": provider_effect_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_delivery_record(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            payload=payload,
            allowed_keys=_DELIVERED_PAYLOAD_KEYS,
        )

    def record_recovered(
        self,
        session_id: str,
        run_id: str,
        *,
        batch_id: str,
        recovered_effect_ids: tuple[str, ...],
        recovered_reads: int = 0,
        recovered_lookups: int = 0,
        lane: str = "",
        operation_id: str = "",
    ) -> None:
        if not batch_id or len(batch_id) > MAX_BATCH_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_id: {batch_id!r}")
        lane = lane or lane_for_run(run_id)
        operation_id = operation_id or operation_id_for_run(run_id)

        clean_effect_ids = [str(eid)[:MAX_EFFECT_ID_CHARS] for eid in recovered_effect_ids if eid]
        rec_reads = max(0, int(recovered_reads))
        rec_lookups = max(0, int(recovered_lookups))

        # Check existing recovered records for this batch in current operation for idempotency
        entries = self._session_log.read(session_id)
        for entry in entries:
            if entry.kind != "operation_effect" or entry.operation_id != operation_id:
                continue
            payload = entry.payload
            if (
                payload.get("effect_kind") == EFFECT_KIND
                and payload.get("record_kind") == RECORD_KIND_RECOVERED
                and str(payload.get("batch_id") or "").strip() == batch_id
            ):
                existing_eids = [str(e) for e in payload.get("recovered_effect_ids", []) if e]
                existing_reads = int(payload.get("recovered_reads", 0))
                existing_lookups = int(payload.get("recovered_lookups", 0))
                if (
                    existing_eids == clean_effect_ids
                    and existing_reads == rec_reads
                    and existing_lookups == rec_lookups
                ):
                    return  # Idempotent no-op
                raise ToolResultDeliveryError(
                    f"conflicting recovered record for batch {batch_id!r}"
                )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "effect_kind": EFFECT_KIND,
            "record_kind": RECORD_KIND_RECOVERED,
            "ref": f"delivery_recovered:{batch_id}",
            "batch_id": batch_id,
            "session_id": session_id,
            "run_id": run_id,
            "lane": lane,
            "operation_id": operation_id,
            "recovered_effect_ids": clean_effect_ids,
            "recovered_reads": rec_reads,
            "recovered_lookups": rec_lookups,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_delivery_record(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            payload=payload,
            allowed_keys=_RECOVERED_PAYLOAD_KEYS,
        )

    def load_batches(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[DeliveryBatchProjection, ...]:
        entries = self._session_log.read(session_id)
        target_op = operation_id_for_run(run_id)

        intents: dict[str, DeliveryBatchIntent] = {}
        ordered_batch_ids: list[str] = []
        send_attempts: dict[str, list[str]] = {}
        delivered: dict[str, list[str]] = {}
        recovered_facts: dict[str, dict[str, Any]] = {}

        for entry in entries:
            if entry.kind != "operation_effect":
                continue
            payload = entry.payload
            if payload.get("effect_kind") != EFFECT_KIND:
                continue
            if entry.operation_id != target_op:
                continue

            rkind = payload.get("record_kind")
            batch_id = str(payload.get("batch_id") or "").strip()
            if not batch_id:
                raise ToolResultDeliveryError("delivery record missing batch_id in current operation")

            if rkind == RECORD_KIND_BATCH_INTENT:
                if batch_id not in intents:
                    raw_items = payload.get("items")
                    if isinstance(raw_items, list) and raw_items:
                        items = tuple(DeliveryBatchItem.from_dict(it) for it in raw_items)
                    else:
                        tool_refs = tuple(str(r) for r in payload.get("tool_refs", ()))
                        tool_names = tuple(str(n) for n in payload.get("tool_names", ()))
                        items = tuple(
                            DeliveryBatchItem(
                                tool_index=idx,
                                tool_name=n,
                                ref=r,
                                replay_class="safe" if is_replayable_safe_tool(n) else "unsafe",
                                is_denied=False,
                            )
                            for idx, (r, n) in enumerate(zip(tool_refs, tool_names, strict=True))
                        )
                    intent = DeliveryBatchIntent(
                        batch_id=batch_id,
                        session_id=str(payload.get("session_id") or ""),
                        run_id=str(payload.get("run_id") or ""),
                        lane=str(payload.get("lane") or ""),
                        operation_id=str(payload.get("operation_id") or ""),
                        turn=int(payload.get("turn") or 0),
                        items=items,
                        batch_digest=str(payload.get("batch_digest") or ""),
                        created_at=str(payload.get("created_at") or ""),
                    )
                    intent.validate()
                    intents[batch_id] = intent
                    ordered_batch_ids.append(batch_id)
            elif rkind == RECORD_KIND_SEND_ATTEMPT:
                peid = str(payload.get("provider_effect_id") or "")
                if peid:
                    send_attempts.setdefault(batch_id, []).append(peid)
            elif rkind == RECORD_KIND_DELIVERED:
                peid = str(payload.get("provider_effect_id") or "")
                if peid:
                    delivered.setdefault(batch_id, []).append(peid)
            elif rkind == RECORD_KIND_RECOVERED:
                if batch_id in recovered_facts:
                    existing = recovered_facts[batch_id]
                    if (
                        list(existing.get("recovered_effect_ids", [])) != list(payload.get("recovered_effect_ids", []))
                        or int(existing.get("recovered_reads", 0)) != int(payload.get("recovered_reads", 0))
                        or int(existing.get("recovered_lookups", 0)) != int(payload.get("recovered_lookups", 0))
                    ):
                        raise ToolResultDeliveryError(f"conflicting recovered facts for batch {batch_id!r}")
                else:
                    recovered_facts[batch_id] = payload

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
        entries = self._session_log.read(session_id)
        target_op = operation_id_for_run(run_id)
        facts_by_batch: dict[str, DeliveryRecoveredFact] = {}
        for entry in entries:
            if entry.kind != "operation_effect":
                continue
            payload = entry.payload
            if payload.get("effect_kind") != EFFECT_KIND or payload.get("record_kind") != RECORD_KIND_RECOVERED:
                continue
            if entry.operation_id != target_op:
                continue
            batch_id = str(payload.get("batch_id") or "").strip()
            if not batch_id:
                raise ToolResultDeliveryError("recovered fact missing batch_id")
            fact = DeliveryRecoveredFact(
                batch_id=batch_id,
                session_id=str(payload.get("session_id") or ""),
                run_id=str(payload.get("run_id") or ""),
                lane=str(payload.get("lane") or ""),
                operation_id=str(payload.get("operation_id") or ""),
                recovered_effect_ids=tuple(str(e) for e in payload.get("recovered_effect_ids", ())),
                recovered_reads=max(0, int(payload.get("recovered_reads", 0))),
                recovered_lookups=max(0, int(payload.get("recovered_lookups", 0))),
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
