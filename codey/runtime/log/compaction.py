"""Log compaction: replay-equivalent spine plus recovery facts.

Only this module decides which records survive compaction. Retention
policy for effect and delivery records lives in the effects domain and
is called from here; grouping and rebatching stay here.
"""

from __future__ import annotations

import uuid

from codey.runtime.log.session_log import RuntimeLogCorruption, RuntimeLogEntry


def _complete_batch_prefix(
    entries: list[RuntimeLogEntry],
) -> list[RuntimeLogEntry]:
    valid: list[RuntimeLogEntry] = []
    index = 0
    while index < len(entries):
        first = entries[index]
        batch_id = first.batch_id
        batch_count = first.batch_count
        batch: list[RuntimeLogEntry] = []
        for offset in range(batch_count):
            row_index = index + offset
            if row_index >= len(entries):
                return valid
            entry = entries[row_index]
            if (
                entry.batch_id != batch_id
                or entry.batch_count != batch_count
                or entry.batch_index != offset
            ):
                if row_index >= len(entries) - 1:
                    return valid
                raise RuntimeLogCorruption("runtime log batch is not contiguous")
            batch.append(entry)
        valid.extend(batch)
        index += batch_count
    return valid


def _compact_entries(
    entries: tuple[RuntimeLogEntry, ...],
) -> tuple[RuntimeLogEntry, ...]:
    """Keep the replay-equivalent task-operation spine and recovery facts."""
    ordered_operations: list[str] = []
    started: dict[str, RuntimeLogEntry] = {}
    latest_state: dict[str, RuntimeLogEntry] = {}
    settled: dict[str, RuntimeLogEntry] = {}
    operation_effects: dict[str, list[RuntimeLogEntry]] = {}
    delivery_effects: dict[str, list[RuntimeLogEntry]] = {}

    for entry in entries:
        if entry.kind == "operation_started":
            if entry.operation_id not in started:
                ordered_operations.append(entry.operation_id)
            started[entry.operation_id] = entry
            continue
        if entry.kind == "operation_state":
            latest_state[entry.operation_id] = entry
            continue
        if entry.kind == "operation_effect":
            effect_kind = entry.payload.get("effect_kind")
            if effect_kind == "runtime_effect":
                operation_effects.setdefault(entry.operation_id, []).append(entry)
            elif effect_kind == "tool_result_delivery":
                delivery_effects.setdefault(entry.operation_id, []).append(entry)
            continue
        if entry.kind == "operation_settled":
            settled[entry.operation_id] = entry
    compacted: list[RuntimeLogEntry] = []
    for operation_id in ordered_operations:
        start = started.get(operation_id)
        if start is None:
            continue
        compacted.append(start)
        state = latest_state.get(operation_id)
        if state is not None:
            compacted.append(state)

        is_open = operation_id not in settled
        raw_effects = operation_effects.get(operation_id, [])
        intents: dict[str, RuntimeLogEntry] = {}
        settlements: dict[str, RuntimeLogEntry] = {}
        ordered_effect_ids: list[str] = []

        for eff in raw_effects:
            eid = str(eff.payload.get("effect_id") or "")
            rkind = eff.payload.get("record_kind")
            if not eid:
                continue
            if rkind == "intent":
                if eid not in intents:
                    ordered_effect_ids.append(eid)
                intents[eid] = eff
            elif rkind == "settlement":
                settlements[eid] = eff

        from codey.runtime.effects.effect_records import keep_effect_pair_for_compaction
        from codey.runtime.effects.tool_result_delivery import keep_delivery_entry_for_compaction

        for eid in ordered_effect_ids:
            intent_entry = intents.get(eid)
            settlement_entry = settlements.get(eid)
            if intent_entry is None:
                continue
            settlement_payload = settlement_entry.payload if settlement_entry is not None else None
            if not keep_effect_pair_for_compaction(
                is_open=is_open,
                settlement_payload=settlement_payload,
            ):
                continue
            compacted.append(intent_entry)
            if settlement_entry is not None:
                compacted.append(settlement_entry)

        raw_deliveries = delivery_effects.get(operation_id, [])
        for deliv in raw_deliveries:
            rkind = deliv.payload.get("record_kind")
            bid = str(deliv.payload.get("batch_id") or "")
            if not bid or not rkind:
                continue
            if not keep_delivery_entry_for_compaction(record_kind=str(rkind), is_open=is_open):
                continue
            compacted.append(deliv)

        finish = settled.get(operation_id)
        if finish is not None:
            compacted.append(finish)
    return _rebatch(compacted)


def _rebatch(entries: list[RuntimeLogEntry]) -> tuple[RuntimeLogEntry, ...]:
    if not entries:
        return ()
    batch_id = f"batch-{uuid.uuid4().hex}"
    batch_count = len(entries)
    return tuple(
        RuntimeLogEntry(
            session_id=entry.session_id,
            lane=entry.lane,
            operation_id=entry.operation_id,
            kind=entry.kind,
            payload=dict(entry.payload),
            entry_id=entry.entry_id,
            created_at=entry.created_at,
            batch_id=batch_id,
            batch_index=index,
            batch_count=batch_count,
        )
        for index, entry in enumerate(entries)
    )
