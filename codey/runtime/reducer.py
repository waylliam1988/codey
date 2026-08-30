"""Replay runtime session logs into operation state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from codey.runtime.outcome import OperationOutcomeStatus
from codey.runtime.session_log import RuntimeLogCorruption, RuntimeLogEntry

_OUTCOMES = {"completed", "failed", "aborted", "suspended"}
_KNOWN_EFFECT_KINDS = {"run_phase"}


@dataclass
class OperationProjection:
    operation_id: str
    lane: str
    kind: str
    status: str = "open"
    outcome: OperationOutcomeStatus | str = ""
    effect_refs: list[str] = field(default_factory=list)


@dataclass
class LaneProjection:
    name: str
    open_operation_id: str = ""
    settled_operation_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeProjection:
    lanes: dict[str, LaneProjection]
    operations: dict[str, OperationProjection]


def reduce_session(entries: tuple[RuntimeLogEntry, ...]) -> RuntimeProjection:
    lanes: dict[str, LaneProjection] = {}
    operations: dict[str, OperationProjection] = {}
    for entry in entries:
        _apply_entry_to(lanes, operations, entry)

    return RuntimeProjection(lanes=lanes, operations=operations)


def apply_entry(
    projection: RuntimeProjection,
    entry: RuntimeLogEntry,
) -> RuntimeProjection:
    return apply_entries(projection, (entry,))


def apply_entries(
    projection: RuntimeProjection,
    entries: Iterable[RuntimeLogEntry],
) -> RuntimeProjection:
    lanes, operations = _clone_projection_parts(projection)
    for entry in entries:
        _apply_entry_to(lanes, operations, entry)
    return RuntimeProjection(lanes=lanes, operations=operations)


def _clone_projection_parts(
    projection: RuntimeProjection,
) -> tuple[dict[str, LaneProjection], dict[str, OperationProjection]]:
    lanes = {
        name: LaneProjection(
            name=lane.name,
            open_operation_id=lane.open_operation_id,
            settled_operation_ids=list(lane.settled_operation_ids),
        )
        for name, lane in projection.lanes.items()
    }
    operations = {
        operation_id: OperationProjection(
            operation_id=operation.operation_id,
            lane=operation.lane,
            kind=operation.kind,
            status=operation.status,
            outcome=operation.outcome,
            effect_refs=list(operation.effect_refs),
        )
        for operation_id, operation in projection.operations.items()
    }
    return lanes, operations


def _apply_entry_to(
    lanes: dict[str, LaneProjection],
    operations: dict[str, OperationProjection],
    entry: RuntimeLogEntry,
) -> None:
    lane = lanes.setdefault(entry.lane, LaneProjection(entry.lane))
    operation = operations.get(entry.operation_id)

    if entry.kind == "operation_started":
        if operation is not None:
            raise RuntimeLogCorruption("operation started twice")
        if lane.open_operation_id:
            raise RuntimeLogCorruption("lane already has an open operation")
        operation_kind = _required_payload_text(entry, "operation_kind")
        lane.open_operation_id = entry.operation_id
        operations[entry.operation_id] = OperationProjection(
            operation_id=entry.operation_id,
            lane=entry.lane,
            kind=operation_kind,
        )
        return

    if operation is None:
        raise RuntimeLogCorruption("operation record without start")
    if operation.status != "open":
        raise RuntimeLogCorruption("operation record after settlement")

    if entry.kind == "operation_effect":
        effect_kind = _required_payload_text(entry, "effect_kind")
        if effect_kind not in _KNOWN_EFFECT_KINDS:
            raise RuntimeLogCorruption("unknown operation effect")
        effect_ref = _required_payload_text(entry, "ref")
        operation.effect_refs.append(effect_ref)
        return

    if entry.kind == "operation_settled":
        outcome = _required_payload_text(entry, "outcome")
        if outcome not in _OUTCOMES:
            raise RuntimeLogCorruption("unknown operation outcome")
        operation.status = "settled"
        operation.outcome = outcome
        lane.open_operation_id = ""
        lane.settled_operation_ids.append(entry.operation_id)
        return

    raise RuntimeLogCorruption("unknown runtime log entry kind")


def _required_payload_text(entry: RuntimeLogEntry, field: str) -> str:
    value = entry.payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeLogCorruption(f"{field} must be a non-empty string")
    return value
