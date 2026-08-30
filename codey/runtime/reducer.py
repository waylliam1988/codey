"""Replay runtime session logs into operation state."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.runtime.outcome import OperationOutcomeStatus
from codey.runtime.session_log import RuntimeLogCorruption, RuntimeLogEntry

_OUTCOMES = {"completed", "failed", "aborted", "suspended"}
_KNOWN_EFFECT_KINDS = {
    "checkpoint_ref",
    "completion_verdict",
    "evidence_ref",
    "observation",
    "receipt_ref",
    "status",
    "trace_ref",
}


@dataclass
class OperationProjection:
    operation_id: str
    lane: str
    kind: str
    status: str = "open"
    outcome: OperationOutcomeStatus | str = ""
    effect_refs: list[str] = field(default_factory=list)
    tool_invocations: list[str] = field(default_factory=list)
    settled_tools: list[str] = field(default_factory=list)


@dataclass
class LaneProjection:
    name: str
    open_operation_id: str = ""
    settled_operation_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeProjection:
    lanes: dict[str, LaneProjection]
    operations: dict[str, OperationProjection]
    ignored_effect_entry_ids: tuple[str, ...] = ()


def reduce_session(entries: tuple[RuntimeLogEntry, ...]) -> RuntimeProjection:
    lanes: dict[str, LaneProjection] = {}
    operations: dict[str, OperationProjection] = {}
    tool_invocations: set[tuple[str, str]] = set()
    settled_tools: set[tuple[str, str]] = set()
    ignored_effects: list[str] = []

    for entry in entries:
        lane = lanes.setdefault(entry.lane, LaneProjection(entry.lane))
        operation = operations.get(entry.operation_id)

        if entry.kind == "operation_started":
            if operation is not None:
                raise RuntimeLogCorruption("operation started twice")
            if lane.open_operation_id:
                raise RuntimeLogCorruption("lane already has an open operation")
            lane.open_operation_id = entry.operation_id
            operations[entry.operation_id] = OperationProjection(
                operation_id=entry.operation_id,
                lane=entry.lane,
                kind=str(entry.payload.get("operation_kind") or "operation"),
            )
            continue

        if operation is None:
            raise RuntimeLogCorruption("operation record without start")
        if operation.status != "open":
            raise RuntimeLogCorruption("operation record after settlement")

        if entry.kind == "operation_effect":
            effect_kind = str(entry.payload.get("effect_kind") or "")
            if effect_kind not in _KNOWN_EFFECT_KINDS:
                ignored_effects.append(entry.entry_id)
                continue
            effect_ref = str(entry.payload.get("ref") or entry.entry_id)
            operation.effect_refs.append(effect_ref)
            continue

        if entry.kind == "tool_invocation":
            invocation_id = _required_payload_text(entry, "invocation_id")
            key = (entry.operation_id, invocation_id)
            if key in tool_invocations:
                raise RuntimeLogCorruption("duplicate tool invocation")
            tool_invocations.add(key)
            operation.tool_invocations.append(invocation_id)
            continue

        if entry.kind == "tool_settled":
            invocation_id = _required_payload_text(entry, "invocation_id")
            key = (entry.operation_id, invocation_id)
            if key not in tool_invocations:
                raise RuntimeLogCorruption("tool settled without invocation")
            if key in settled_tools:
                raise RuntimeLogCorruption("tool settled twice")
            settled_tools.add(key)
            operation.settled_tools.append(invocation_id)
            continue

        if entry.kind == "operation_settled":
            outcome = _required_payload_text(entry, "outcome")
            if outcome not in _OUTCOMES:
                raise RuntimeLogCorruption("unknown operation outcome")
            operation.status = "settled"
            operation.outcome = outcome
            lane.open_operation_id = ""
            lane.settled_operation_ids.append(entry.operation_id)
            continue

        raise RuntimeLogCorruption("unknown runtime log entry kind")

    return RuntimeProjection(
        lanes=lanes,
        operations=operations,
        ignored_effect_entry_ids=tuple(ignored_effects),
    )


def _required_payload_text(entry: RuntimeLogEntry, field: str) -> str:
    value = entry.payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeLogCorruption(f"{field} must be a non-empty string")
    return value
