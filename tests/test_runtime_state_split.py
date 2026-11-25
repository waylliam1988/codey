"""Contract tests for durable runtime state, delivery snapshots and prompt surfaces."""

from __future__ import annotations

import unittest

from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.core.operation_state import new_operation_state
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    DeliveryBatchProjection,
    ToolResultDeliveryError,
    compute_batch_digest,
    delivery_batches_from_snapshot,
    mark_delivered,
    mark_send_attempt,
)
from codey.runtime.observe.prompt_surface import (
    build_prompt_surface_record,
    validate_prompt_surface_payload,
)
from codey.runtime.write.checkpoint import delivery_snapshot

_SESSION = "sess-b5"
_RUN = "run-b5"


def _valid_surface_payload() -> dict[str, object]:
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


class DeliverySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        items = (DeliveryBatchItem(tool_index=0, tool_name="read", ref="eff-1", replay_class="safe"),)
        self.intent = DeliveryBatchIntent(
            batch_id="batch-b5-1", session_id=_SESSION, run_id=_RUN, turn=1,
            items=items, batch_digest=compute_batch_digest(items), created_at="2026-01-01T00:00:00Z",
        )

    def _load(self, snapshots: object) -> tuple[DeliveryBatchProjection, ...]:
        return delivery_batches_from_snapshot(snapshots, session_id=_SESSION, run_id=_RUN)

    def test_empty_checkpoint(self) -> None:
        self.assertEqual(self._load([]), ())

    def test_pending_batch_remains_undelivered(self) -> None:
        [batch] = self._load([delivery_snapshot(DeliveryBatchProjection(self.intent))])
        self.assertEqual(batch.intent.batch_id, self.intent.batch_id)
        self.assertFalse(batch.is_delivered)

    def test_attempt_then_delivered_survives_checkpoint(self) -> None:
        pending = DeliveryBatchProjection(self.intent)
        attempted = mark_send_attempt(batch_id=self.intent.batch_id, provider_effect_id="pe-1", batches=(pending,))
        assert attempted is not None
        delivered = mark_delivered(batch_id=self.intent.batch_id, provider_effect_id="pe-1", batches=(attempted,))
        assert delivered is not None
        [restored] = self._load([delivery_snapshot(delivered)])
        self.assertEqual(restored.send_attempts, ("pe-1",))
        self.assertTrue(restored.is_delivered)
        self.assertEqual(restored.delivered_effect_ids, ("pe-1",))

    def test_delivery_without_attempt_rejected(self) -> None:
        corrupt = DeliveryBatchProjection(self.intent, delivered_effect_ids=("pe-1",))
        with self.assertRaises(ToolResultDeliveryError):
            self._load([delivery_snapshot(corrupt)])


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

if __name__ == "__main__":
    unittest.main()
