"""PLR-split B5 parity tests: lock behavior of the four refactored functions.

Pure-extraction guard for:
- RuntimeOperationState.from_payload (operation_state.py)
- batches_from_entries (tool_result_delivery.py)
- _compact_entries (compaction.py)
- validate_prompt_surface_payload (prompt_surface.py)

No bug-fix tests here: any deterministic bug found during the split must
first get a failing test in this file (red), then a product-code fix (green).
Uncertain observations are recorded in the task report only.
"""

from __future__ import annotations

import unittest

from codey.runtime.core.operation_state import (
    RuntimeOperationState,
    lane_for_run,
    new_operation_state,
    operation_id_for_run,
)
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryError,
    batches_from_entries,
)
from codey.runtime.log.compaction import _compact_entries
from codey.runtime.log.entries import RuntimeLogEntry
from codey.runtime.observe.prompt_surface import (
    build_prompt_surface_record,
    validate_prompt_surface_payload,
)

_SESSION = "sess-b5"
_RUN = "run-b5"


def _entry(*, lane: str, operation_id: str, kind: str, payload: dict) -> RuntimeLogEntry:
    return RuntimeLogEntry(
        session_id=_SESSION,
        lane=lane,
        operation_id=operation_id,
        kind=kind,
        payload=payload,
    )


def _delivery_items() -> tuple[DeliveryBatchItem, ...]:
    from codey.runtime.effects.tool_result_delivery import compute_batch_digest

    items = (
        DeliveryBatchItem(tool_index=0, tool_name="read", ref="eff-1", replay_class="safe"),
        DeliveryBatchItem(tool_index=1, tool_name="search", ref="eff-2", replay_class="safe"),
    )
    digest = compute_batch_digest(items)
    return items, digest


def _intent_payload(batch_id: str = "batch-b5-1") -> dict:
    items, digest = _delivery_items()
    intent = DeliveryBatchIntent(
        batch_id=batch_id,
        session_id=_SESSION,
        run_id=_RUN,
        turn=1,
        items=items,
        batch_digest=digest,
    )
    return intent.to_payload()


def _valid_surface_payload() -> dict:
    record = build_prompt_surface_record(
        phase="writer",
        send_ref="effect_b5",
        prompt_digest="sha256:" + "d" * 64,
        prompt_chars=100,
        epoch_id="ctx_epoch:" + "e" * 16,
        sections=(),
    )
    return {
        "schema_version": 1,
        "surface_id": record.surface_id,
        "send_ref": record.send_ref,
        "phase": record.phase,
        "prompt_digest": record.prompt_digest,
        "prompt_chars": record.prompt_chars,
        "epoch_id": record.epoch_id,
        "model_tool_contract_hash": "",
        "runtime_tool_contract_hash": "",
        "sections": [],
    }


class OperationStateFromPayloadParityTests(unittest.TestCase):
    def test_round_trip_fresh_state(self) -> None:
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        restored = RuntimeOperationState.from_payload(state.to_payload())
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.to_payload(), state.to_payload())

    def test_non_dict_and_bad_envelope_rejected(self) -> None:
        self.assertIsNone(RuntimeOperationState.from_payload(None))
        self.assertIsNone(RuntimeOperationState.from_payload([]))
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        bad_kind = dict(state.to_payload())
        bad_kind["kind"] = "nope"
        self.assertIsNone(RuntimeOperationState.from_payload(bad_kind))

    def test_unknown_keys_and_unknown_leaf_rejected(self) -> None:
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        extra = dict(state.to_payload())
        extra["zzz_unknown"] = 1
        self.assertIsNone(RuntimeOperationState.from_payload(extra))
        bad_leaf = dict(state.to_payload())
        bad_leaf["leaf"] = "nope"
        self.assertIsNone(RuntimeOperationState.from_payload(bad_leaf))

    def test_accepted_must_be_fresh(self) -> None:
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        stale = dict(state.to_payload())
        stale["writer_attempt"] = 2
        self.assertIsNone(RuntimeOperationState.from_payload(stale))

    def test_non_terminal_must_not_carry_terminal(self) -> None:
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        payload = dict(state.to_payload())
        payload["terminal"] = {"stop_reason": "done"}
        self.assertIsNone(RuntimeOperationState.from_payload(payload))


class BatchesFromEntriesParityTests(unittest.TestCase):
    def _lane_op(self) -> tuple[str, str]:
        return lane_for_run(_RUN), operation_id_for_run(_RUN)

    def test_empty_entries(self) -> None:
        self.assertEqual(
            batches_from_entries((), session_id=_SESSION, run_id=_RUN),
            (),
        )

    def test_intent_only_projection(self) -> None:
        lane, op = self._lane_op()
        entries = (
            _entry(lane=lane, operation_id=op, kind="operation_effect", payload=_intent_payload()),
        )
        batches = batches_from_entries(entries, session_id=_SESSION, run_id=_RUN)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].intent.batch_id, "batch-b5-1")
        self.assertFalse(batches[0].is_delivered)

    def test_attempt_then_delivered_lifecycle(self) -> None:
        from codey.runtime.effects.tool_result_delivery import delivered_entry, send_attempt_entry

        lane, op = self._lane_op()
        intent_entry = _entry(lane=lane, operation_id=op, kind="operation_effect", payload=_intent_payload())
        entries = (intent_entry,)
        first = batches_from_entries(entries, session_id=_SESSION, run_id=_RUN)
        attempt = send_attempt_entry(
            _SESSION, _RUN, batch_id="batch-b5-1", provider_effect_id="pe-1", batches=first
        )
        assert attempt is not None
        attempt_entry = _entry(
            lane=str(attempt["lane"]),
            operation_id=str(attempt["operation_id"]),
            kind="operation_effect",
            payload=dict(attempt["payload"]),
        )
        second = batches_from_entries(entries + (attempt_entry,), session_id=_SESSION, run_id=_RUN)
        self.assertEqual(second[0].send_attempts, ("pe-1",))
        delivered = delivered_entry(
            _SESSION, _RUN, batch_id="batch-b5-1", provider_effect_id="pe-1", batches=second
        )
        assert delivered is not None
        delivered_entry_row = _entry(
            lane=str(delivered["lane"]),
            operation_id=str(delivered["operation_id"]),
            kind="operation_effect",
            payload=dict(delivered["payload"]),
        )
        final = batches_from_entries(
            entries + (attempt_entry, delivered_entry_row), session_id=_SESSION, run_id=_RUN
        )
        self.assertTrue(final[0].is_delivered)
        self.assertEqual(final[0].delivered_effect_ids, ("pe-1",))

    def test_orphan_attempt_rejected(self) -> None:
        lane, op = self._lane_op()
        payload = {
            "schema_version": 1,
            "effect_kind": "tool_result_delivery",
            "record_kind": "send_attempt",
            "ref": "delivery_attempt:batch-ghost:pe-x",
            "batch_id": "batch-ghost",
            "session_id": _SESSION,
            "run_id": _RUN,
            "lane": lane,
            "operation_id": op,
            "provider_effect_id": "pe-x",
            "created_at": "2026-01-01T00:00:00Z",
        }
        entries = (_entry(lane=lane, operation_id=op, kind="operation_effect", payload=payload),)
        with self.assertRaises(ToolResultDeliveryError):
            batches_from_entries(entries, session_id=_SESSION, run_id=_RUN)


class CompactEntriesParityTests(unittest.TestCase):
    def _op_entries(self) -> tuple[str, str]:
        return lane_for_run(_RUN), operation_id_for_run(_RUN)

    def test_keeps_spine_drops_intermediate_state(self) -> None:
        lane, op = self._op_entries()
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        entries = (
            _entry(lane=lane, operation_id=op, kind="operation_started", payload={"operation_kind": "task"}),
            _entry(lane=lane, operation_id=op, kind="operation_state", payload=dict(state.to_payload())),
            _entry(lane=lane, operation_id=op, kind="operation_state", payload=dict(state.to_payload())),
        )
        compacted = _compact_entries(entries)
        self.assertEqual([e.kind for e in compacted], ["operation_started", "operation_state"])
        self.assertEqual([e.batch_index for e in compacted], [0, 1])
        self.assertEqual(compacted[0].batch_count, 2)

    def test_open_operation_keeps_pairs_and_delivery(self) -> None:
        lane, op = self._op_entries()
        entries = (
            _entry(lane=lane, operation_id=op, kind="operation_started", payload={"operation_kind": "task"}),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "record_kind": "intent", "effect_id": "e1"},
            ),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "record_kind": "settlement", "effect_id": "e1", "status": "ok"},
            ),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "tool_result_delivery", "record_kind": "batch_intent", "batch_id": "b1"},
            ),
        )
        compacted = _compact_entries(entries)
        kinds = [(e.kind, e.payload.get("record_kind")) for e in compacted]
        self.assertEqual(
            kinds,
            [
                ("operation_started", None),
                ("operation_effect", "intent"),
                ("operation_effect", "settlement"),
                ("operation_effect", "batch_intent"),
            ],
        )

    def test_closed_operation_drops_ok_pair_and_intent_keeps_recovered(self) -> None:
        lane, op = self._op_entries()
        state = new_operation_state(
            session_id=_SESSION,
            run_id=_RUN,
            project="",
            provider_id="prov",
            turn_budget=5,
            max_repair_rounds=1,
        )
        entries = (
            _entry(lane=lane, operation_id=op, kind="operation_started", payload={"operation_kind": "task"}),
            _entry(lane=lane, operation_id=op, kind="operation_state", payload=dict(state.to_payload())),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "record_kind": "intent", "effect_id": "e1"},
            ),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "record_kind": "settlement", "effect_id": "e1", "status": "ok"},
            ),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "tool_result_delivery", "record_kind": "batch_intent", "batch_id": "b1"},
            ),
            _entry(
                lane=lane,
                operation_id=op,
                kind="operation_effect",
                payload={"effect_kind": "tool_result_delivery", "record_kind": "recovered", "batch_id": "b1"},
            ),
            _entry(lane=lane, operation_id=op, kind="operation_settled", payload={"status": "closed"}),
        )
        compacted = _compact_entries(entries)
        kinds = [(e.kind, e.payload.get("record_kind")) for e in compacted]
        self.assertEqual(
            kinds,
            [
                ("operation_started", None),
                ("operation_state", None),
                ("operation_effect", "recovered"),
                ("operation_settled", None),
            ],
        )


class PromptSurfaceParityTests(unittest.TestCase):
    def test_valid_record_passes(self) -> None:
        self.assertTrue(validate_prompt_surface_payload(_valid_surface_payload()))

    def test_forbidden_key_rejected(self) -> None:
        payload = _valid_surface_payload()
        payload["prompt"] = "raw text must never validate"
        self.assertFalse(validate_prompt_surface_payload(payload))

    def test_tampered_surface_id_rejected(self) -> None:
        payload = _valid_surface_payload()
        payload["surface_id"] = "prompt_surface:" + "0" * 16
        self.assertFalse(validate_prompt_surface_payload(payload))

    def test_bad_section_digest_rejected(self) -> None:
        payload = _valid_surface_payload()
        payload["sections"] = [{"name": "s", "digest": "nope", "chars": 3}]
        self.assertFalse(validate_prompt_surface_payload(payload))

    def test_non_mapping_rejected(self) -> None:
        self.assertFalse(validate_prompt_surface_payload("nope"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
