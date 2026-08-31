"""Runtime external effect intent, settlement, and recovery projection.

The session log is the durable fact source. This module records bounded intents
before executing real external effects (provider sends, tool executions, repair
rounds) and records settlements after completion or error. On crash recovery,
pending intents are projected and settled with synthetic recovery records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeSessionLog
from codey.storage.local_store import session_key

SCHEMA_VERSION = 1
EFFECT_KIND = "runtime_effect"

RECORD_KIND_INTENT = "intent"
RECORD_KIND_SETTLEMENT = "settlement"
RECORD_KINDS = frozenset({RECORD_KIND_INTENT, RECORD_KIND_SETTLEMENT})

EFFECT_CATEGORY_PROVIDER_SEND = "provider_send"
EFFECT_CATEGORY_TOOL_CALL = "tool_call"
EFFECT_CATEGORY_REPAIR_ROUND = "repair_round"
EFFECT_CATEGORIES = frozenset({
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    EFFECT_CATEGORY_REPAIR_ROUND,
})

SETTLEMENT_STATUS_OK = "ok"
SETTLEMENT_STATUS_ERROR = "error"
SETTLEMENT_STATUS_INTERRUPTED = "interrupted"
SETTLEMENT_STATUS_DENIED = "denied"
SETTLEMENT_STATUSES = frozenset({
    SETTLEMENT_STATUS_OK,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_INTERRUPTED,
    SETTLEMENT_STATUS_DENIED,
})

SENT_STATE_NEVER_SENT = "never_sent"
SENT_STATE_MAYBE_SENT = "maybe_sent"
SENT_STATE_SETTLED = "settled"
SENT_STATES = frozenset({
    SENT_STATE_NEVER_SENT,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_SETTLED,
})

MAX_EFFECT_ID_CHARS = 100
MAX_TEXT_CHARS = 120
MAX_REF_CHARS = 160


class RuntimeEffectError(Exception):
    """Base error for runtime effect violations."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute_args_digest(args: Any) -> str:
    """Compute a bounded deterministic digest of arguments without storing raw text."""
    if args is None:
        return ""
    if isinstance(args, str):
        content = args.encode("utf-8")
    else:
        try:
            content = json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")
        except Exception:
            content = str(args).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()[:16]}"


@dataclass(frozen=True)
class RuntimeEffectIntent:
    effect_id: str
    effect_category: str
    session_id: str
    run_id: str
    lane: str = "task"
    operation_id: str = ""
    phase: str = ""
    provider_id: str = ""
    turn: int = 0
    tool_index: int = 0
    tool_name: str = ""
    tool_id: str = ""
    args_digest: str = ""
    display_ref: str = ""
    replay_class: str = ReplayClass.UNSAFE
    created_at: str = ""
    record_kind: str = RECORD_KIND_INTENT
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.effect_id or not isinstance(self.effect_id, str):
            raise RuntimeEffectError("effect_id must be a non-empty string")
        if self.effect_category not in EFFECT_CATEGORIES:
            raise RuntimeEffectError(f"unknown effect_category: {self.effect_category}")
        if self.record_kind != RECORD_KIND_INTENT:
            raise RuntimeEffectError("record_kind must be 'intent'")
        if self.replay_class not in (ReplayClass.SAFE, ReplayClass.UNSAFE):
            raise RuntimeEffectError("invalid replay_class")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "effect_kind": EFFECT_KIND,
            "record_kind": self.record_kind,
            "ref": f"effect:{self.effect_id}",
            "effect_id": self.effect_id,
            "effect_category": self.effect_category,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.operation_id,
            "phase": self.phase,
            "provider_id": self.provider_id,
            "turn": self.turn,
            "tool_index": self.tool_index,
            "tool_name": self.tool_name,
            "tool_id": self.tool_id,
            "args_digest": self.args_digest,
            "display_ref": self.display_ref,
            "replay_class": self.replay_class,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RuntimeEffectIntent:
        if payload.get("effect_kind") != EFFECT_KIND or payload.get("record_kind") != RECORD_KIND_INTENT:
            raise RuntimeEffectError("invalid payload for RuntimeEffectIntent")
        return cls(
            effect_id=str(payload.get("effect_id") or ""),
            effect_category=str(payload.get("effect_category") or ""),
            session_id=str(payload.get("session_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            lane=str(payload.get("lane") or "task"),
            operation_id=str(payload.get("operation_id") or ""),
            phase=str(payload.get("phase") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            turn=int(payload.get("turn") or 0),
            tool_index=int(payload.get("tool_index") or 0),
            tool_name=str(payload.get("tool_name") or ""),
            tool_id=str(payload.get("tool_id") or ""),
            args_digest=str(payload.get("args_digest") or ""),
            display_ref=str(payload.get("display_ref") or ""),
            replay_class=str(payload.get("replay_class") or ReplayClass.UNSAFE),
            created_at=str(payload.get("created_at") or ""),
            record_kind=RECORD_KIND_INTENT,
            schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class RuntimeEffectSettlement:
    effect_id: str
    effect_category: str
    session_id: str
    run_id: str
    status: str = SETTLEMENT_STATUS_OK
    lane: str = "task"
    operation_id: str = ""
    error_code: str = ""
    sent_state: str = SENT_STATE_SETTLED
    replay_class: str = ReplayClass.UNSAFE
    created_at: str = ""
    record_kind: str = RECORD_KIND_SETTLEMENT
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.effect_id or not isinstance(self.effect_id, str):
            raise RuntimeEffectError("effect_id must be a non-empty string")
        if self.effect_category not in EFFECT_CATEGORIES:
            raise RuntimeEffectError(f"unknown effect_category: {self.effect_category}")
        if self.record_kind != RECORD_KIND_SETTLEMENT:
            raise RuntimeEffectError("record_kind must be 'settlement'")
        if self.status not in SETTLEMENT_STATUSES:
            raise RuntimeEffectError(f"unknown settlement status: {self.status}")
        if self.sent_state not in SENT_STATES:
            raise RuntimeEffectError(f"unknown sent_state: {self.sent_state}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "effect_kind": EFFECT_KIND,
            "record_kind": self.record_kind,
            "ref": f"effect_settlement:{self.effect_id}",
            "effect_id": self.effect_id,
            "effect_category": self.effect_category,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.operation_id,
            "status": self.status,
            "error_code": self.error_code,
            "sent_state": self.sent_state,
            "replay_class": self.replay_class,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RuntimeEffectSettlement:
        if payload.get("effect_kind") != EFFECT_KIND or payload.get("record_kind") != RECORD_KIND_SETTLEMENT:
            raise RuntimeEffectError("invalid payload for RuntimeEffectSettlement")
        return cls(
            effect_id=str(payload.get("effect_id") or ""),
            effect_category=str(payload.get("effect_category") or ""),
            session_id=str(payload.get("session_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            status=str(payload.get("status") or SETTLEMENT_STATUS_OK),
            lane=str(payload.get("lane") or "task"),
            operation_id=str(payload.get("operation_id") or ""),
            error_code=str(payload.get("error_code") or ""),
            sent_state=str(payload.get("sent_state") or SENT_STATE_SETTLED),
            replay_class=str(payload.get("replay_class") or ReplayClass.UNSAFE),
            created_at=str(payload.get("created_at") or ""),
            record_kind=RECORD_KIND_SETTLEMENT,
            schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class RuntimeEffectProjection:
    intent: RuntimeEffectIntent
    settlement: RuntimeEffectSettlement | None = None

    @property
    def is_pending(self) -> bool:
        return self.settlement is None

    @property
    def is_settled(self) -> bool:
        return self.settlement is not None


@dataclass(frozen=True)
class RecoverySummary:
    interrupted_writes: int = 0
    unconfirmed_provider_calls: int = 0
    retryable_reads: int = 0
    interrupted_repairs: int = 0
    explanation_lines: tuple[str, ...] = ()


class RuntimeEffectStore:
    """Store and recovery projection for external effect intents and settlements."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def _default_operation_id(self, run_id: str) -> str:
        return f"task:{session_key(run_id)}"

    def record_intent(
        self,
        session_id: str,
        run_id: str,
        intent: RuntimeEffectIntent,
    ) -> RuntimeEffectIntent:
        """Record an intent before executing a real effect."""
        op_id = intent.operation_id or self._default_operation_id(run_id)
        created_at = intent.created_at or _now()
        prepared = RuntimeEffectIntent(
            effect_id=intent.effect_id,
            effect_category=intent.effect_category,
            session_id=session_id,
            run_id=run_id,
            lane=intent.lane,
            operation_id=op_id,
            phase=intent.phase,
            provider_id=intent.provider_id,
            turn=intent.turn,
            tool_index=intent.tool_index,
            tool_name=intent.tool_name,
            tool_id=intent.tool_id,
            args_digest=intent.args_digest,
            display_ref=intent.display_ref,
            replay_class=intent.replay_class,
            created_at=created_at,
        )
        self.session_log.append(
            session_id=session_id,
            lane=prepared.lane,
            operation_id=prepared.operation_id,
            kind="operation_effect",
            payload=prepared.to_payload(),
        )
        return prepared

    def record_settlement(
        self,
        session_id: str,
        run_id: str,
        settlement: RuntimeEffectSettlement,
    ) -> RuntimeEffectSettlement:
        """Record a settlement after an effect has completed or failed."""
        op_id = settlement.operation_id or self._default_operation_id(run_id)
        created_at = settlement.created_at or _now()
        prepared = RuntimeEffectSettlement(
            effect_id=settlement.effect_id,
            effect_category=settlement.effect_category,
            session_id=session_id,
            run_id=run_id,
            status=settlement.status,
            lane=settlement.lane,
            operation_id=op_id,
            error_code=settlement.error_code,
            sent_state=settlement.sent_state,
            replay_class=settlement.replay_class,
            created_at=created_at,
        )
        self.session_log.append(
            session_id=session_id,
            lane=prepared.lane,
            operation_id=prepared.operation_id,
            kind="operation_effect",
            payload=prepared.to_payload(),
        )
        return prepared

    def load_effects(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[RuntimeEffectProjection, ...]:
        """Load and aggregate all effect intent/settlement pairs for a run."""
        entries = self.session_log.entries(session_id)
        intents: dict[str, RuntimeEffectIntent] = {}
        settlements: dict[str, RuntimeEffectSettlement] = {}
        ordered_effect_ids: list[str] = []

        for entry in entries:
            if entry.kind != "operation_effect":
                continue
            payload = entry.payload
            if payload.get("effect_kind") != EFFECT_KIND or payload.get("run_id") != run_id:
                continue
            record_kind = payload.get("record_kind")
            effect_id = str(payload.get("effect_id") or "")
            if not effect_id:
                continue
            if record_kind == RECORD_KIND_INTENT:
                if effect_id not in intents:
                    ordered_effect_ids.append(effect_id)
                intents[effect_id] = RuntimeEffectIntent.from_payload(payload)
            elif record_kind == RECORD_KIND_SETTLEMENT:
                settlements[effect_id] = RuntimeEffectSettlement.from_payload(payload)

        projections: list[RuntimeEffectProjection] = []
        for eid in ordered_effect_ids:
            intent = intents.get(eid)
            if intent is not None:
                projections.append(RuntimeEffectProjection(
                    intent=intent,
                    settlement=settlements.get(eid),
                ))
        return tuple(projections)

    def pending_effects(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[RuntimeEffectProjection, ...]:
        """Return all effects that have an intent but no settlement yet."""
        return tuple(p for p in self.load_effects(session_id, run_id) if p.is_pending)

    def synthesize_interrupted(
        self,
        session_id: str,
        run_id: str,
        effect_id: str,
        reason: str = "interrupted_by_crash",
        *,
        replay_class: str = ReplayClass.UNSAFE,
        sent_state: str = SENT_STATE_MAYBE_SENT,
    ) -> RuntimeEffectSettlement:
        """Synthesize an interrupted settlement for an unclosed effect intent."""
        effects = self.load_effects(session_id, run_id)
        matching = next((p for p in effects if p.intent.effect_id == effect_id), None)
        if matching is None:
            raise RuntimeEffectError(f"intent not found for effect_id: {effect_id}")
        if matching.settlement is not None:
            return matching.settlement

        category = matching.intent.effect_category
        resolved_sent_state = (
            sent_state
            if category == EFFECT_CATEGORY_PROVIDER_SEND
            else SENT_STATE_SETTLED
        )
        resolved_replay_class = matching.intent.replay_class or replay_class

        settlement = RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=category,
            session_id=session_id,
            run_id=run_id,
            status=SETTLEMENT_STATUS_INTERRUPTED,
            lane=matching.intent.lane,
            operation_id=matching.intent.operation_id,
            error_code=reason,
            sent_state=resolved_sent_state,
            replay_class=resolved_replay_class,
            created_at=_now(),
        )
        return self.record_settlement(session_id, run_id, settlement)

    def recovery_summary(
        self,
        session_id: str,
        run_id: str,
    ) -> RecoverySummary:
        """Derive quiet user-facing recovery explanation rows for a run."""
        effects = self.load_effects(session_id, run_id)
        interrupted_writes = 0
        unconfirmed_provider_calls = 0
        retryable_reads = 0
        interrupted_repairs = 0

        for proj in effects:
            intent = proj.intent
            settlement = proj.settlement
            status = settlement.status if settlement else SETTLEMENT_STATUS_INTERRUPTED

            if intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND:
                if status == SETTLEMENT_STATUS_INTERRUPTED or (settlement and settlement.sent_state == SENT_STATE_MAYBE_SENT):
                    unconfirmed_provider_calls += 1
            elif intent.effect_category == EFFECT_CATEGORY_REPAIR_ROUND:
                if status == SETTLEMENT_STATUS_INTERRUPTED:
                    interrupted_repairs += 1
            elif intent.effect_category == EFFECT_CATEGORY_TOOL_CALL:
                if status == SETTLEMENT_STATUS_INTERRUPTED:
                    if intent.replay_class == ReplayClass.SAFE:
                        retryable_reads += 1
                    else:
                        interrupted_writes += 1

        lines: list[str] = []
        if interrupted_writes > 0:
            lines.append("Local write was interrupted and was not repeated")
        if unconfirmed_provider_calls > 0:
            lines.append("Provider response was not confirmed")
        if retryable_reads > 0:
            lines.append("Read action can be retried")
        if interrupted_repairs > 0:
            lines.append("Repair round was interrupted")

        return RecoverySummary(
            interrupted_writes=interrupted_writes,
            unconfirmed_provider_calls=unconfirmed_provider_calls,
            retryable_reads=retryable_reads,
            interrupted_repairs=interrupted_repairs,
            explanation_lines=tuple(lines),
        )


__all__ = [
    "EFFECT_CATEGORIES",
    "EFFECT_CATEGORY_PROVIDER_SEND",
    "EFFECT_CATEGORY_REPAIR_ROUND",
    "EFFECT_CATEGORY_TOOL_CALL",
    "EFFECT_KIND",
    "RECORD_KINDS",
    "RECORD_KIND_INTENT",
    "RECORD_KIND_SETTLEMENT",
    "RecoverySummary",
    "RuntimeEffectError",
    "RuntimeEffectIntent",
    "RuntimeEffectProjection",
    "RuntimeEffectSettlement",
    "RuntimeEffectStore",
    "SENT_STATES",
    "SENT_STATE_MAYBE_SENT",
    "SENT_STATE_NEVER_SENT",
    "SENT_STATE_SETTLED",
    "SETTLEMENT_STATUSES",
    "SETTLEMENT_STATUS_DENIED",
    "SETTLEMENT_STATUS_ERROR",
    "SETTLEMENT_STATUS_INTERRUPTED",
    "SETTLEMENT_STATUS_OK",
    "compute_args_digest",
]
