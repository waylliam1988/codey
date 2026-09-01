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
import uuid

from codey.runtime.effects import lane_for_run, operation_id_for_run
from codey.runtime.replay_policy import (
    ReplayClass,
    is_replayable_safe_tool,
)
from codey.runtime.replay_args import validate_replay_args_shape
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
SETTLEMENT_STATUSES = frozenset({
    SETTLEMENT_STATUS_OK,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_INTERRUPTED,
})

SENT_STATE_MAYBE_SENT = "maybe_sent"
SENT_STATE_SETTLED = "settled"
SENT_STATES = frozenset({
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_SETTLED,
})

MAX_EFFECT_ID_CHARS = 128
MAX_TEXT_CHARS = 120
MAX_REF_CHARS = 160
MAX_ERROR_CODE_CHARS = 80
MAX_ARGS_DIGEST_CHARS = 64

_INTENT_PAYLOAD_KEYS = frozenset({
    "schema_version",
    "effect_kind",
    "record_kind",
    "ref",
    "effect_id",
    "effect_category",
    "session_id",
    "run_id",
    "lane",
    "operation_id",
    "phase",
    "provider_id",
    "turn",
    "tool_index",
    "tool_name",
    "tool_id",
    "args_digest",
    "display_ref",
    "replay_class",
    "replay_args",
    "created_at",
})
_SETTLEMENT_PAYLOAD_KEYS = frozenset({
    "schema_version",
    "effect_kind",
    "record_kind",
    "ref",
    "effect_id",
    "effect_category",
    "session_id",
    "run_id",
    "lane",
    "operation_id",
    "status",
    "error_code",
    "sent_state",
    "replay_class",
    "replay_count",
    "replayed_from_effect_id",
    "created_at",
})



class RuntimeEffectError(Exception):
    """Base error for runtime effect violations."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_effect_id(category: str, run_id: str) -> str:
    """Generate a globally unique effect id for each distinct execution attempt."""
    short_cat = (category or "eff")[:12]
    safe_run = session_key(run_id) or "run"
    return f"eff_{short_cat}_{safe_run}_{uuid.uuid4().hex[:12]}"


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


def _require_bounded_str(val: Any, name: str, max_len: int, *, allow_empty: bool = False) -> str:
    if not isinstance(val, str):
        raise RuntimeEffectError(f"{name} must be a str")
    if not allow_empty and not val:
        raise RuntimeEffectError(f"{name} must not be empty")
    if len(val) > max_len:
        raise RuntimeEffectError(f"{name} exceeds max length {max_len}")
    return val


def _require_nonnegative_int(val: Any, name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise RuntimeEffectError(f"{name} must be an integer")
    if val < 0:
        raise RuntimeEffectError(f"{name} must be non-negative")
    return val


def _require_enum_str(val: Any, name: str, allowed: frozenset[str] | tuple[str, ...]) -> str:
    if not isinstance(val, str) or not val:
        raise RuntimeEffectError(f"missing or invalid {name}: {val}")
    if val not in allowed:
        raise RuntimeEffectError(f"unknown {name}: {val}")
    return val


def _reject_unknown_payload_keys(payload: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise RuntimeEffectError(f"unknown effect payload keys: {', '.join(unknown)}")


def _require_ref(payload: dict[str, Any], expected: str) -> None:
    actual = _require_bounded_str(payload.get("ref"), "ref", MAX_REF_CHARS)
    if actual != expected:
        raise RuntimeEffectError(f"effect ref '{actual}' does not match expected ref '{expected}'")


def _replay_args_from_payload(
    *,
    effect_category: str,
    replay_class: str,
    tool_name: str,
    raw_replay_args: Any,
) -> dict[str, object] | None:
    if raw_replay_args is None:
        return None
    if (
        effect_category != EFFECT_CATEGORY_TOOL_CALL
        or replay_class != ReplayClass.SAFE
        or not is_replayable_safe_tool(tool_name)
    ):
        return None
    if not isinstance(raw_replay_args, dict) or isinstance(raw_replay_args, bool):
        return None
    try:
        validate_replay_args_shape(tool_name, raw_replay_args)
    except ValueError:
        return None
    return dict(raw_replay_args)


@dataclass(frozen=True)
class RuntimeEffectIntent:
    effect_id: str
    effect_category: str
    session_id: str
    run_id: str
    lane: str = ""
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
    replay_args: dict[str, object] | None = None
    created_at: str = ""
    record_kind: str = RECORD_KIND_INTENT
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise RuntimeEffectError(f"invalid schema_version: {self.schema_version}")
        _require_bounded_str(self.effect_id, "effect_id", MAX_EFFECT_ID_CHARS)
        _require_bounded_str(self.session_id, "session_id", MAX_REF_CHARS)
        _require_bounded_str(self.run_id, "run_id", MAX_REF_CHARS)
        _require_bounded_str(self.lane, "lane", MAX_REF_CHARS, allow_empty=True)
        _require_bounded_str(self.operation_id, "operation_id", MAX_REF_CHARS, allow_empty=True)
        _require_bounded_str(self.phase, "phase", MAX_TEXT_CHARS, allow_empty=True)
        _require_bounded_str(self.provider_id, "provider_id", MAX_TEXT_CHARS, allow_empty=True)
        _require_bounded_str(self.tool_name, "tool_name", MAX_TEXT_CHARS, allow_empty=True)
        _require_bounded_str(self.tool_id, "tool_id", MAX_TEXT_CHARS, allow_empty=True)
        _require_bounded_str(self.args_digest, "args_digest", MAX_ARGS_DIGEST_CHARS, allow_empty=True)
        _require_bounded_str(self.display_ref, "display_ref", MAX_TEXT_CHARS, allow_empty=True)
        _require_bounded_str(self.created_at, "created_at", MAX_TEXT_CHARS, allow_empty=True)
        _require_nonnegative_int(self.turn, "turn")
        _require_nonnegative_int(self.tool_index, "tool_index")
        _require_enum_str(self.effect_category, "effect_category", EFFECT_CATEGORIES)
        if self.record_kind != RECORD_KIND_INTENT:
            raise RuntimeEffectError("record_kind must be 'intent'")
        _require_enum_str(self.replay_class, "replay_class", (ReplayClass.SAFE, ReplayClass.UNSAFE))
        if self.replay_args is not None:
            if not isinstance(self.replay_args, dict) or isinstance(self.replay_args, bool):
                raise RuntimeEffectError("replay_args must be a dict or None")
            if (
                self.effect_category != EFFECT_CATEGORY_TOOL_CALL
                or self.replay_class != ReplayClass.SAFE
                or not is_replayable_safe_tool(self.tool_name)
            ):
                raise RuntimeEffectError("replay_args only permitted for replayable safe tool calls")
            try:
                validate_replay_args_shape(self.tool_name, self.replay_args)
            except ValueError as exc:
                raise RuntimeEffectError(f"invalid replay_args: {exc}") from exc

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.replay_args is not None:
            payload["replay_args"] = dict(self.replay_args)
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RuntimeEffectIntent:
        if not isinstance(payload, dict):
            raise RuntimeEffectError("payload must be a dict")
        _reject_unknown_payload_keys(payload, _INTENT_PAYLOAD_KEYS)
        if payload.get("effect_kind") != EFFECT_KIND or payload.get("record_kind") != RECORD_KIND_INTENT:
            raise RuntimeEffectError("invalid payload kind for RuntimeEffectIntent")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION or isinstance(version, bool):
            raise RuntimeEffectError(f"unsupported schema version: {version}")
        effect_id = _require_bounded_str(payload.get("effect_id"), "effect_id", MAX_EFFECT_ID_CHARS)
        _require_ref(payload, f"effect:{effect_id}")
        effect_category = _require_enum_str(payload.get("effect_category"), "effect_category", EFFECT_CATEGORIES)
        tool_name = _require_bounded_str(payload.get("tool_name") or "", "tool_name", MAX_TEXT_CHARS, allow_empty=True)
        replay_class = _require_enum_str(payload.get("replay_class"), "replay_class", (ReplayClass.SAFE, ReplayClass.UNSAFE))
        replay_args = _replay_args_from_payload(
            effect_category=effect_category,
            replay_class=replay_class,
            tool_name=tool_name,
            raw_replay_args=payload.get("replay_args"),
        )

        return cls(
            effect_id=effect_id,
            effect_category=effect_category,
            session_id=_require_bounded_str(payload.get("session_id"), "session_id", MAX_REF_CHARS),
            run_id=_require_bounded_str(payload.get("run_id"), "run_id", MAX_REF_CHARS),
            lane=_require_bounded_str(payload.get("lane"), "lane", MAX_REF_CHARS),
            operation_id=_require_bounded_str(payload.get("operation_id"), "operation_id", MAX_REF_CHARS),
            phase=_require_bounded_str(payload.get("phase") or "", "phase", MAX_TEXT_CHARS, allow_empty=True),
            provider_id=_require_bounded_str(payload.get("provider_id") or "", "provider_id", MAX_TEXT_CHARS, allow_empty=True),
            turn=_require_nonnegative_int(payload.get("turn"), "turn"),
            tool_index=_require_nonnegative_int(payload.get("tool_index"), "tool_index"),
            tool_name=tool_name,
            tool_id=_require_bounded_str(payload.get("tool_id") or "", "tool_id", MAX_TEXT_CHARS, allow_empty=True),
            args_digest=_require_bounded_str(payload.get("args_digest") or "", "args_digest", MAX_ARGS_DIGEST_CHARS, allow_empty=True),
            display_ref=_require_bounded_str(payload.get("display_ref") or "", "display_ref", MAX_TEXT_CHARS, allow_empty=True),
            replay_class=replay_class,
            replay_args=replay_args,
            created_at=_require_bounded_str(payload.get("created_at") or "", "created_at", MAX_TEXT_CHARS, allow_empty=True),
            record_kind=RECORD_KIND_INTENT,
            schema_version=SCHEMA_VERSION,
        )


@dataclass(frozen=True)
class RuntimeEffectSettlement:
    effect_id: str
    effect_category: str
    session_id: str
    run_id: str
    status: str = SETTLEMENT_STATUS_OK
    lane: str = ""
    operation_id: str = ""
    error_code: str = ""
    sent_state: str = SENT_STATE_SETTLED
    replay_class: str = ReplayClass.UNSAFE
    replay_count: int = 0
    replayed_from_effect_id: str = ""
    created_at: str = ""
    record_kind: str = RECORD_KIND_SETTLEMENT
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise RuntimeEffectError(f"invalid schema_version: {self.schema_version}")
        _require_bounded_str(self.effect_id, "effect_id", MAX_EFFECT_ID_CHARS)
        _require_bounded_str(self.session_id, "session_id", MAX_REF_CHARS)
        _require_bounded_str(self.run_id, "run_id", MAX_REF_CHARS)
        _require_bounded_str(self.lane, "lane", MAX_REF_CHARS, allow_empty=True)
        _require_bounded_str(self.operation_id, "operation_id", MAX_REF_CHARS, allow_empty=True)
        _require_bounded_str(self.error_code, "error_code", MAX_ERROR_CODE_CHARS, allow_empty=True)
        _require_bounded_str(self.created_at, "created_at", MAX_TEXT_CHARS, allow_empty=True)
        _require_nonnegative_int(self.replay_count, "replay_count")
        _require_bounded_str(self.replayed_from_effect_id, "replayed_from_effect_id", MAX_EFFECT_ID_CHARS, allow_empty=True)
        if self.replayed_from_effect_id and self.replayed_from_effect_id != self.effect_id:
            raise RuntimeEffectError(
                f"replayed_from_effect_id '{self.replayed_from_effect_id}' must match effect_id '{self.effect_id}'"
            )
        _require_enum_str(self.effect_category, "effect_category", EFFECT_CATEGORIES)
        if self.record_kind != RECORD_KIND_SETTLEMENT:
            raise RuntimeEffectError("record_kind must be 'settlement'")
        _require_enum_str(self.status, "status", SETTLEMENT_STATUSES)
        _require_enum_str(self.sent_state, "sent_state", SENT_STATES)
        _require_enum_str(self.replay_class, "replay_class", (ReplayClass.SAFE, ReplayClass.UNSAFE))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
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
        if self.replay_count:
            payload["replay_count"] = self.replay_count
        if self.replayed_from_effect_id:
            payload["replayed_from_effect_id"] = self.replayed_from_effect_id
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RuntimeEffectSettlement:
        if not isinstance(payload, dict):
            raise RuntimeEffectError("payload must be a dict")
        _reject_unknown_payload_keys(payload, _SETTLEMENT_PAYLOAD_KEYS)
        if payload.get("effect_kind") != EFFECT_KIND or payload.get("record_kind") != RECORD_KIND_SETTLEMENT:
            raise RuntimeEffectError("invalid payload kind for RuntimeEffectSettlement")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION or isinstance(version, bool):
            raise RuntimeEffectError(f"unsupported schema version: {version}")
        effect_id = _require_bounded_str(payload.get("effect_id"), "effect_id", MAX_EFFECT_ID_CHARS)
        _require_ref(payload, f"effect_settlement:{effect_id}")

        replay_count = _require_nonnegative_int(payload.get("replay_count", 0), "replay_count")
        replayed_from_effect_id = _require_bounded_str(
            payload.get("replayed_from_effect_id") or "",
            "replayed_from_effect_id",
            MAX_EFFECT_ID_CHARS,
            allow_empty=True,
        )

        return cls(
            effect_id=effect_id,
            effect_category=_require_enum_str(payload.get("effect_category"), "effect_category", EFFECT_CATEGORIES),
            session_id=_require_bounded_str(payload.get("session_id"), "session_id", MAX_REF_CHARS),
            run_id=_require_bounded_str(payload.get("run_id"), "run_id", MAX_REF_CHARS),
            status=_require_enum_str(payload.get("status"), "status", SETTLEMENT_STATUSES),
            lane=_require_bounded_str(payload.get("lane"), "lane", MAX_REF_CHARS),
            operation_id=_require_bounded_str(payload.get("operation_id"), "operation_id", MAX_REF_CHARS),
            error_code=_require_bounded_str(payload.get("error_code") or "", "error_code", MAX_ERROR_CODE_CHARS, allow_empty=True),
            sent_state=_require_enum_str(payload.get("sent_state"), "sent_state", SENT_STATES),
            replay_class=_require_enum_str(payload.get("replay_class"), "replay_class", (ReplayClass.SAFE, ReplayClass.UNSAFE)),
            replay_count=replay_count,
            replayed_from_effect_id=replayed_from_effect_id,
            created_at=_require_bounded_str(payload.get("created_at") or "", "created_at", MAX_TEXT_CHARS, allow_empty=True),
            record_kind=RECORD_KIND_SETTLEMENT,
            schema_version=SCHEMA_VERSION,
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
    replayed_reads: int = 0
    replayed_searches: int = 0
    explanation_lines: tuple[str, ...] = ()



def record_settlement_safely(
    store: Any,
    session_id: str,
    run_id: str,
    settlement: RuntimeEffectSettlement,
) -> RuntimeEffectSettlement | None:
    """Safely record settlement without masking real business outcomes/errors."""
    if store is None or not session_id or not run_id or not settlement.effect_id:
        return None
    try:
        return store.record_settlement(session_id, run_id, settlement)
    except Exception:
        return None


class RuntimeEffectStore:
    """Store and recovery projection for external effect intents and settlements."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def record_intent(
        self,
        session_id: str,
        run_id: str,
        intent: RuntimeEffectIntent,
    ) -> RuntimeEffectIntent:
        """Record an intent before executing a real effect."""
        if intent.session_id != session_id:
            raise RuntimeEffectError(
                f"intent session_id '{intent.session_id}' does not match expected session_id '{session_id}'"
            )
        if intent.run_id != run_id:
            raise RuntimeEffectError(
                f"intent run_id '{intent.run_id}' does not match expected run_id '{run_id}'"
            )
        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)
        if intent.lane and intent.lane != expected_lane:
            raise RuntimeEffectError(
                f"intent lane '{intent.lane}' does not match expected lane '{expected_lane}' for run {run_id}"
            )
        if intent.operation_id and intent.operation_id != expected_op:
            raise RuntimeEffectError(
                f"intent operation_id '{intent.operation_id}' does not match expected operation_id '{expected_op}' for run {run_id}"
            )
        lane = intent.lane or expected_lane
        op_id = intent.operation_id or expected_op
        created_at = intent.created_at or _now()
        prepared = RuntimeEffectIntent(
            effect_id=intent.effect_id,
            effect_category=intent.effect_category,
            session_id=session_id,
            run_id=run_id,
            lane=lane,
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
            replay_args=intent.replay_args,
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
        if settlement.session_id != session_id:
            raise RuntimeEffectError(
                f"settlement session_id '{settlement.session_id}' does not match expected session_id '{session_id}'"
            )
        if settlement.run_id != run_id:
            raise RuntimeEffectError(
                f"settlement run_id '{settlement.run_id}' does not match expected run_id '{run_id}'"
            )
        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)
        if settlement.lane and settlement.lane != expected_lane:
            raise RuntimeEffectError(
                f"settlement lane '{settlement.lane}' does not match expected lane '{expected_lane}' for run {run_id}"
            )
        if settlement.operation_id and settlement.operation_id != expected_op:
            raise RuntimeEffectError(
                f"settlement operation_id '{settlement.operation_id}' does not match expected operation_id '{expected_op}' for run {run_id}"
            )

        effects = self.load_effects(session_id, run_id)
        matching = next((p for p in effects if p.intent.effect_id == settlement.effect_id), None)
        if matching is None:
            raise RuntimeEffectError(f"cannot record settlement for unknown intent: {settlement.effect_id}")

        if settlement.effect_category != matching.intent.effect_category:
            raise RuntimeEffectError(
                f"settlement category '{settlement.effect_category}' does not match intent category '{matching.intent.effect_category}'"
            )

        if matching.settlement is not None:
            # Already settled: idempotent return if identical, else raise
            if (
                matching.settlement.status == settlement.status
                and matching.settlement.error_code == settlement.error_code
                and matching.settlement.sent_state == settlement.sent_state
                and matching.settlement.replay_class == settlement.replay_class
                and matching.settlement.replay_count == settlement.replay_count
                and matching.settlement.replayed_from_effect_id == settlement.replayed_from_effect_id
            ):
                return matching.settlement
            raise RuntimeEffectError(f"effect already settled: {settlement.effect_id}")

        lane = settlement.lane or matching.intent.lane or expected_lane
        op_id = settlement.operation_id or matching.intent.operation_id or expected_op
        created_at = settlement.created_at or _now()
        prepared = RuntimeEffectSettlement(
            effect_id=settlement.effect_id,
            effect_category=matching.intent.effect_category,
            session_id=session_id,
            run_id=run_id,
            status=settlement.status,
            lane=lane,
            operation_id=op_id,
            error_code=settlement.error_code,
            sent_state=settlement.sent_state,
            replay_class=matching.intent.replay_class,
            replay_count=settlement.replay_count,
            replayed_from_effect_id=settlement.replayed_from_effect_id,
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
        """Load and aggregate all effect intent/settlement pairs for a run in strict chronological order."""
        entries = self.session_log.entries(session_id)
        expected_lane = lane_for_run(run_id)
        expected_op = operation_id_for_run(run_id)
        intents: dict[str, RuntimeEffectIntent] = {}
        settlements: dict[str, RuntimeEffectSettlement] = {}
        ordered_effect_ids: list[str] = []

        for entry in entries:
            if entry.kind != "operation_effect":
                continue
            payload = entry.payload
            if not isinstance(payload, dict):
                continue
            if payload.get("effect_kind") != EFFECT_KIND:
                continue

            # Strict validation against run operation lane, operation_id, and session_id boundary
            is_current_operation = (entry.lane == expected_lane or entry.operation_id == expected_op)
            is_current_run = (payload.get("run_id") == run_id)
            if not is_current_operation and not is_current_run:
                continue

            if entry.lane != expected_lane:
                raise RuntimeEffectError(
                    f"entry lane '{entry.lane}' does not match expected lane '{expected_lane}' for run {run_id}"
                )
            if entry.operation_id != expected_op:
                raise RuntimeEffectError(
                    f"entry operation_id '{entry.operation_id}' does not match expected operation_id '{expected_op}' for run {run_id}"
                )
            payload_session_id = payload.get("session_id")
            if not payload_session_id or payload_session_id != session_id:
                raise RuntimeEffectError(
                    f"payload session_id '{payload_session_id}' does not match expected session_id '{session_id}' for run {run_id}"
                )
            payload_run_id = payload.get("run_id")
            if not payload_run_id or payload_run_id != run_id:
                raise RuntimeEffectError(
                    f"payload run_id '{payload_run_id}' does not match expected run_id '{run_id}'"
                )
            payload_lane = payload.get("lane")
            if not payload_lane or payload_lane != expected_lane:
                raise RuntimeEffectError(
                    f"payload lane '{payload_lane}' does not match expected lane '{expected_lane}' for run {run_id}"
                )
            payload_op = payload.get("operation_id")
            if not payload_op or payload_op != expected_op:
                raise RuntimeEffectError(
                    f"payload operation_id '{payload_op}' does not match expected operation_id '{expected_op}' for run {run_id}"
                )

            record_kind = payload.get("record_kind")
            effect_id = str(payload.get("effect_id") or "")
            if not effect_id:
                raise RuntimeEffectError("missing effect_id in effect payload")

            if record_kind == RECORD_KIND_INTENT:
                if effect_id in intents:
                    raise RuntimeEffectError(f"duplicate intent in session log: {effect_id}")
                ordered_effect_ids.append(effect_id)
                intents[effect_id] = RuntimeEffectIntent.from_payload(payload)

            elif record_kind == RECORD_KIND_SETTLEMENT:
                if effect_id not in intents:
                    raise RuntimeEffectError(f"orphan settlement without intent in session log: {effect_id}")
                new_settlement = RuntimeEffectSettlement.from_payload(payload)
                if new_settlement.effect_category != intents[effect_id].effect_category:
                    raise RuntimeEffectError(
                        f"settlement category '{new_settlement.effect_category}' does not match intent category '{intents[effect_id].effect_category}'"
                    )
                if effect_id in settlements:
                    existing = settlements[effect_id]
                    if (
                        existing.status == new_settlement.status
                        and existing.error_code == new_settlement.error_code
                        and existing.sent_state == new_settlement.sent_state
                        and existing.effect_category == new_settlement.effect_category
                        and existing.replay_class == new_settlement.replay_class
                        and existing.replay_count == new_settlement.replay_count
                        and existing.replayed_from_effect_id == new_settlement.replayed_from_effect_id
                    ):
                        continue
                    raise RuntimeEffectError(f"conflicting duplicate settlement in session log: {effect_id}")
                settlements[effect_id] = new_settlement

            else:
                raise RuntimeEffectError(f"unknown record_kind: {record_kind}")

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
        replayed_reads = 0
        replayed_searches = 0

        for proj in effects:
            intent = proj.intent
            settlement = proj.settlement
            if settlement is None:
                continue

            if settlement.replay_count > 0:
                if intent.tool_name == "read":
                    replayed_reads += 1
                elif intent.tool_name in {"ls", "search", "references"}:
                    replayed_searches += 1
                continue

            if settlement.status != SETTLEMENT_STATUS_INTERRUPTED:
                continue

            if intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND:
                unconfirmed_provider_calls += 1
            elif intent.effect_category == EFFECT_CATEGORY_REPAIR_ROUND:
                interrupted_repairs += 1
            elif intent.effect_category == EFFECT_CATEGORY_TOOL_CALL:
                if intent.replay_class == ReplayClass.SAFE:
                    retryable_reads += 1
                else:
                    interrupted_writes += 1

        lines: list[str] = []
        if replayed_reads > 0:
            lines.append("Read action was recovered")
        if replayed_searches > 0:
            lines.append("Search action was recovered")
        if retryable_reads > 0:
            lines.append("Read action can be retried")
        if interrupted_writes > 0:
            lines.append("Local write was interrupted and was not repeated")
        if unconfirmed_provider_calls > 0:
            lines.append("Provider response was not confirmed")
        if interrupted_repairs > 0:
            lines.append("Repair round was interrupted")

        return RecoverySummary(
            interrupted_writes=interrupted_writes,
            unconfirmed_provider_calls=unconfirmed_provider_calls,
            retryable_reads=retryable_reads,
            interrupted_repairs=interrupted_repairs,
            replayed_reads=replayed_reads,
            replayed_searches=replayed_searches,
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
    "SENT_STATE_SETTLED",
    "SETTLEMENT_STATUSES",
    "SETTLEMENT_STATUS_ERROR",
    "SETTLEMENT_STATUS_INTERRUPTED",
    "SETTLEMENT_STATUS_OK",
    "compute_args_digest",
    "new_effect_id",
    "record_settlement_safely",
]
