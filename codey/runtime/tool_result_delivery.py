"""Runtime tool result delivery tracking, receipts, and projection.

The session log is the durable fact source. This module records bounded receipts
around the delivery of tool results to model providers:
1. batch_intent: recorded before tool execution or before result sending
2. send_attempt: recorded when a provider send effect starts
3. delivered: recorded when the provider request settles successfully
4. recovered: recorded when safe results are reconstructed on resume

Strictly bounded: never records raw result text, prompts, replies, stdout,
stderr, diffs, or source bodies.

Does not import agents, provider, ghost, operations, or tool runners.
"""

from __future__ import annotations

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
    """Raised when delivery receipt data violates schema or bounds."""


@dataclass(frozen=True)
class DeliveryBatchIntent:
    batch_id: str
    session_id: str
    run_id: str
    turn: int
    tool_refs: tuple[str, ...]
    tool_names: tuple[str, ...]
    batch_digest: str
    lane: str = ""
    operation_id: str = ""
    created_at: str = ""

    def validate(self) -> None:
        if not self.batch_id or len(self.batch_id) > MAX_BATCH_ID_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_id: {self.batch_id!r}")
        if not self.session_id:
            raise ToolResultDeliveryError("session_id must not be empty")
        if not self.run_id:
            raise ToolResultDeliveryError("run_id must not be empty")
        if self.turn < 0:
            raise ToolResultDeliveryError(f"turn must be non-negative: {self.turn}")
        if len(self.tool_refs) > MAX_TOOL_REFS:
            raise ToolResultDeliveryError("tool_refs exceed maximum count")
        for ref in self.tool_refs:
            if not isinstance(ref, str) or len(ref) > MAX_REF_CHARS:
                raise ToolResultDeliveryError(f"invalid tool_ref: {ref!r}")
        for name in self.tool_names:
            if not isinstance(name, str) or not name.strip():
                raise ToolResultDeliveryError(f"invalid tool_name: {name!r}")
        if not self.batch_digest or len(self.batch_digest) > MAX_DIGEST_CHARS:
            raise ToolResultDeliveryError(f"invalid batch_digest: {self.batch_digest!r}")

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
            "tool_refs": list(self.tool_refs),
            "tool_names": list(self.tool_names),
            "batch_digest": self.batch_digest,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
        }


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
        if not self.intent.tool_names:
            return False
        return all(is_replayable_safe_tool(name) for name in self.intent.tool_names)


def compute_batch_digest(tool_refs: tuple[str, ...], tool_names: tuple[str, ...]) -> str:
    parts = [f"{n}:{r}" for n, r in zip(tool_names, tool_refs)]
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


class ToolResultDeliveryStore:
    """Appends delivery receipts to session log and projects batch states."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self._session_log = session_log

    @property
    def session_log(self) -> RuntimeSessionLog:
        return self._session_log

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
                tool_refs=intent.tool_refs,
                tool_names=intent.tool_names,
                batch_digest=intent.batch_digest,
                created_at=intent.created_at,
            )
        _check_no_forbidden_keys(intent.to_payload())
        payload = intent.to_payload()
        if not _INTENT_PAYLOAD_KEYS.issuperset(payload.keys()):
            extra = set(payload.keys()) - _INTENT_PAYLOAD_KEYS
            raise ToolResultDeliveryError(f"unknown payload fields: {extra}")

        self._session_log.append(
            session_id=session_id,
            lane=intent.lane,
            operation_id=intent.operation_id,
            kind="operation_effect",
            payload=payload,
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
        _check_no_forbidden_keys(payload)

        self._session_log.append(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            kind="operation_effect",
            payload=payload,
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
        _check_no_forbidden_keys(payload)

        self._session_log.append(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            kind="operation_effect",
            payload=payload,
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
            "recovered_reads": max(0, int(recovered_reads)),
            "recovered_lookups": max(0, int(recovered_lookups)),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _check_no_forbidden_keys(payload)

        self._session_log.append(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            kind="operation_effect",
            payload=payload,
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
                continue

            if rkind == RECORD_KIND_BATCH_INTENT:
                if batch_id not in intents:
                    try:
                        intent = DeliveryBatchIntent(
                            batch_id=batch_id,
                            session_id=str(payload.get("session_id") or ""),
                            run_id=str(payload.get("run_id") or ""),
                            lane=str(payload.get("lane") or ""),
                            operation_id=str(payload.get("operation_id") or ""),
                            turn=int(payload.get("turn") or 0),
                            tool_refs=tuple(str(r) for r in payload.get("tool_refs", ())),
                            tool_names=tuple(str(n) for n in payload.get("tool_names", ())),
                            batch_digest=str(payload.get("batch_digest") or ""),
                            created_at=str(payload.get("created_at") or ""),
                        )
                        intent.validate()
                        intents[batch_id] = intent
                        ordered_batch_ids.append(batch_id)
                    except Exception:
                        pass
            elif rkind == RECORD_KIND_SEND_ATTEMPT:
                peid = str(payload.get("provider_effect_id") or "")
                if peid:
                    send_attempts.setdefault(batch_id, []).append(peid)
            elif rkind == RECORD_KIND_DELIVERED:
                peid = str(payload.get("provider_effect_id") or "")
                if peid:
                    delivered.setdefault(batch_id, []).append(peid)
            elif rkind == RECORD_KIND_RECOVERED:
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

    def undelivered_replayable_batches(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[DeliveryBatchProjection, ...]:
        """Return undelivered batches where every tool call is replayable safe."""
        batches = self.load_batches(session_id, run_id)
        return tuple(
            b for b in batches
            if not b.is_delivered and b.is_all_safe
        )


__all__ = [
    "EFFECT_KIND",
    "RECORD_KIND_BATCH_INTENT",
    "RECORD_KIND_DELIVERED",
    "RECORD_KIND_RECOVERED",
    "RECORD_KIND_SEND_ATTEMPT",
    "DeliveryBatchIntent",
    "DeliveryBatchProjection",
    "ToolResultDeliveryError",
    "ToolResultDeliveryStore",
    "compute_batch_digest",
    "new_batch_id",
]
