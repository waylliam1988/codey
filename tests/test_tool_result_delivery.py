from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codey.agents.prompt_context import append_coding_context
from codey.agents.request import AgentRequest
from codey.agents.result_delivery import (
    build_next_tool_prompt,
    deliver_turn_results,
    ensure_result_batch_intent,
)
from codey.agents.state import AgentLoopSession, LoopProgress, LoopStagnation, LoopVerification
from codey.agents.tool_execution import (
    ToolResultDeliveryItem,
    TurnState,
)
from codey.agents.tool_turn import execute_turn_tools
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.operations.recovery import recover_effects_for_resume
from codey.policies.permissions import profile_for_name
from codey.protocols import JsonToolCodec
from codey.runs.details import load_run_details
from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    SETTLEMENT_STATUS_OK,
    new_effect_id,
)
from codey.runtime.effects import RuntimeOperationStore, lane_for_run, operation_id_for_run
from codey.runtime.models import ToolCall, ToolResult
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryError,
    ToolResultDeliveryStore,
    compute_batch_digest,
    new_batch_id,
)


class MockDeliveryProvider:
    name = "MockDeliveryProvider"

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or ['{"tool":"done","args":{"summary":"ok"}}'])
        self.prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return '{"tool":"done","args":{"summary":"ok"}}'


class ToolResultDeliveryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name) / "state"
        self.log = RuntimeSessionLog(self.state_dir)
        self.store = ToolResultDeliveryStore(self.log)
        self.session_id = "sess-deliv-1"
        self.run_id = "run-deliv-1"
        self.operations = RuntimeOperationStore(self.log)
        self.operations.start(
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.temp_dir.name),
            provider_id="mock_provider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_rejects_raw_content_and_oversized_fields(self) -> None:
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="ref1",
                replay_class="safe",
                is_denied=False,
            ),
        )
        intent = DeliveryBatchIntent(
            batch_id="batch-1",
            session_id=self.session_id,
            run_id=self.run_id,
            lane=lane_for_run(self.run_id),
            operation_id=operation_id_for_run(self.run_id),
            turn=1,
            items=items,
            batch_digest=compute_batch_digest(items),
        )
        # Invalid empty session_id
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchIntent(
                batch_id="batch-1",
                session_id="",
                run_id=self.run_id,
                lane="lane-1",
                operation_id="op-1",
                turn=1,
                items=(),
                batch_digest="d",
            ).validate()

        # Try validating payload with raw result key
        raw_payload = intent.to_payload()
        raw_payload["raw_result"] = "some secret code text"
        with self.assertRaises(ToolResultDeliveryError):
            from codey.runtime.tool_result_delivery import _check_no_forbidden_keys
            _check_no_forbidden_keys(raw_payload)

    def test_schema_hygiene_strict_checks(self) -> None:
        # Empty items
        with self.assertRaises(ToolResultDeliveryError):
            compute_batch_digest(())

        # Mismatched digest
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="ref1",
                replay_class="safe",
                is_denied=False,
            ),
        )
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchIntent(
                batch_id="batch-1",
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest="wrong_digest",
            ).validate()

        # Unknown field in record append
        with self.assertRaises(ToolResultDeliveryError):
            self.store._append_delivery_record(
                session_id=self.session_id,
                lane="lane-1",
                operation_id="op-1",
                payload={"schema_version": 1, "extra_bad_field": 123},
                allowed_keys=frozenset({"schema_version"}),
            )

    def test_item_schema_strictness_and_type_validation(self) -> None:
        # Missing keys
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchItem.from_dict({"tool_index": 0, "tool_name": "read"})

        # Extra unknown keys
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchItem.from_dict({
                "tool_index": 0,
                "tool_name": "read",
                "ref": "ref1",
                "replay_class": "safe",
                "is_denied": False,
                "unknown_key": "bad",
            })

        # Bool as tool_index (isinstance(True, int) is True, but type is bool)
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchItem.from_dict({
                "tool_index": True,
                "tool_name": "read",
                "ref": "ref1",
                "replay_class": "safe",
                "is_denied": False,
            })

        # Non-bool as is_denied
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchItem.from_dict({
                "tool_index": 0,
                "tool_name": "read",
                "ref": "ref1",
                "replay_class": "safe",
                "is_denied": 1,
            })

    def test_unknown_record_kind_in_log_fails_closed(self) -> None:
        lane = lane_for_run(self.run_id)
        op_id = operation_id_for_run(self.run_id)
        # Append unknown bogus record_kind
        self.log.append(
            session_id=self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "tool_result_delivery",
                "record_kind": "bogus_kind",
                "ref": "delivery_bogus:batch-bogus",
                "batch_id": "batch-bogus",
            },
        )
        with self.assertRaises(ToolResultDeliveryError):
            self.store.load_batches(self.session_id, self.run_id)
        with self.assertRaises(ToolResultDeliveryError):
            self.store.load_recovered_facts(self.session_id, self.run_id)

    def test_missing_or_corrupt_fields_in_log_fails_closed(self) -> None:
        lane = lane_for_run(self.run_id)
        op_id = operation_id_for_run(self.run_id)
        # Corrupt send_attempt missing provider_effect_id
        self.log.append(
            session_id=self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "tool_result_delivery",
                "record_kind": "send_attempt",
                "ref": "delivery_attempt:b1:p1",
                "batch_id": "b1",
                "session_id": self.session_id,
                "run_id": self.run_id,
                "lane": lane,
                "operation_id": op_id,
                # missing provider_effect_id
                "created_at": "2026-09-02T12:00:00Z",
            },
        )
        with self.assertRaises(ToolResultDeliveryError):
            self.store.load_batches(self.session_id, self.run_id)

    def test_run_boundary_mismatch_fails_closed(self) -> None:
        lane = lane_for_run(self.run_id)
        op_id = operation_id_for_run(self.run_id)

        # Record with mismatched session_id in payload
        self.log.append(
            session_id=self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "tool_result_delivery",
                "record_kind": "batch_intent",
                "ref": "delivery_intent:b-mismatch",
                "batch_id": "b-mismatch",
                "session_id": "wrong-sess",
                "run_id": self.run_id,
                "lane": lane,
                "operation_id": op_id,
                "turn": 1,
                "items": [{"tool_index": 0, "tool_name": "read", "ref": "r1", "replay_class": "safe", "is_denied": False}],
                "batch_digest": "some_digest",
                "created_at": "2026-09-02T12:00:00Z",
            },
        )
        with self.assertRaises(ToolResultDeliveryError):
            self.store.load_batches(self.session_id, self.run_id)

    def test_record_batch_intent_rejects_conflicting_coordinates(self) -> None:
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="ref1",
                replay_class="safe",
                is_denied=False,
            ),
        )
        # Intent has mismatched run_id
        intent = DeliveryBatchIntent(
            batch_id="batch-mismatch",
            session_id=self.session_id,
            run_id="wrong_run_id",
            turn=1,
            items=items,
            batch_digest=compute_batch_digest(items),
        )
        with self.assertRaises(ToolResultDeliveryError):
            self.store.record_batch_intent(self.session_id, self.run_id, intent)

    def test_orphan_send_attempt_and_delivered_rejected(self) -> None:
        lane = lane_for_run(self.run_id)
        op_id = operation_id_for_run(self.run_id)

        # Inject send_attempt without prior batch_intent
        self.log.append(
            session_id=self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "tool_result_delivery",
                "record_kind": "send_attempt",
                "ref": "delivery_attempt:orphan-1:eff-p1",
                "batch_id": "orphan-1",
                "session_id": self.session_id,
                "run_id": self.run_id,
                "lane": lane,
                "operation_id": op_id,
                "provider_effect_id": "eff-p1",
                "created_at": "2026-09-02T12:00:00Z",
            },
        )
        with self.assertRaises(ToolResultDeliveryError):
            self.store.load_batches(self.session_id, self.run_id)

    def test_record_recovered_bounded_str_strictness(self) -> None:
        batch_id = "batch-rec-strict"
        # Overlong effect id
        with self.assertRaises(ToolResultDeliveryError):
            self.store.record_recovered(
                self.session_id,
                self.run_id,
                batch_id=batch_id,
                recovered_effect_ids=("a" * 200,),
            )
        # Empty effect id
        with self.assertRaises(ToolResultDeliveryError):
            self.store.record_recovered(
                self.session_id,
                self.run_id,
                batch_id=batch_id,
                recovered_effect_ids=("",),
            )
        # Non-string effect id
        with self.assertRaises(ToolResultDeliveryError):
            self.store.record_recovered(
                self.session_id,
                self.run_id,
                batch_id=batch_id,
                recovered_effect_ids=(123,),  # type: ignore
            )

    def test_conflicting_duplicate_batch_intent_rejected(self) -> None:
        items1 = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="ref1",
                replay_class="safe",
                is_denied=False,
            ),
        )
        intent1 = DeliveryBatchIntent(
            batch_id="batch-conflict",
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            items=items1,
            batch_digest=compute_batch_digest(items1),
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent1)

        # Identical intent -> safe
        self.store.record_batch_intent(self.session_id, self.run_id, intent1)
        batches = self.store.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)

        # Conflicting intent with same batch_id -> raises error
        items2 = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="search",
                ref="ref2",
                replay_class="safe",
                is_denied=False,
            ),
        )
        intent2 = DeliveryBatchIntent(
            batch_id="batch-conflict",
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            items=items2,
            batch_digest=compute_batch_digest(items2),
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent2)
        with self.assertRaises(ToolResultDeliveryError):
            self.store.load_batches(self.session_id, self.run_id)

    def test_record_recovered_idempotency_and_conflict_rejection(self) -> None:
        batch_id = "batch-idempotent-1"
        self.store.record_recovered(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            recovered_effect_ids=("eff-1",),
            recovered_reads=1,
            recovered_lookups=0,
        )
        # Same record -> idempotent no-op
        self.store.record_recovered(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            recovered_effect_ids=("eff-1",),
            recovered_reads=1,
            recovered_lookups=0,
        )
        facts = self.store.load_recovered_facts(self.session_id, self.run_id)
        self.assertEqual(len(facts), 1)

        # Conflicting record -> raises ToolResultDeliveryError
        with self.assertRaises(ToolResultDeliveryError):
            self.store.record_recovered(
                self.session_id,
                self.run_id,
                batch_id=batch_id,
                recovered_effect_ids=("eff-1",),
                recovered_reads=2,  # Conflicting read count!
                recovered_lookups=0,
            )

    def test_batch_lifecycle_intent_attempt_delivered(self) -> None:
        batch_id = "batch-life-1"
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="eff-1",
                replay_class="safe",
                is_denied=False,
            ),
            DeliveryBatchItem(
                tool_index=1,
                tool_name="search",
                ref="eff-2",
                replay_class="safe",
                is_denied=False,
            ),
        )
        digest = compute_batch_digest(items)

        intent = DeliveryBatchIntent(
            batch_id=batch_id,
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            items=items,
            batch_digest=digest,
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent)

        batches = self.store.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)
        b0 = batches[0]
        self.assertEqual(b0.intent.batch_id, batch_id)
        self.assertFalse(b0.is_delivered)
        self.assertTrue(b0.is_all_safe)
        self.assertTrue(b0.can_recover_before_provider_send)
        self.assertEqual(len(self.store.undelivered_replayable_batches(self.session_id, self.run_id)), 1)

        # Record send attempt
        self.store.record_send_attempt(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            provider_effect_id="eff-send-100",
        )
        b_after_attempt = self.store.load_batches(self.session_id, self.run_id)[0]
        self.assertEqual(b_after_attempt.send_attempts, ("eff-send-100",))
        self.assertFalse(b_after_attempt.is_delivered)
        # Attempted batch MUST NOT be replayable before provider send!
        self.assertFalse(b_after_attempt.can_recover_before_provider_send)
        self.assertEqual(len(self.store.undelivered_replayable_batches(self.session_id, self.run_id)), 0)

        # Record delivered
        self.store.record_delivered(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            provider_effect_id="eff-send-100",
        )
        b_after_deliv = self.store.load_batches(self.session_id, self.run_id)[0]
        self.assertTrue(b_after_deliv.is_delivered)
        self.assertEqual(b_after_deliv.delivered_effect_ids, ("eff-send-100",))
        self.assertEqual(len(self.store.undelivered_replayable_batches(self.session_id, self.run_id)), 0)

    def test_unsafe_and_policy_denied_batch_not_in_undelivered_replayable(self) -> None:
        batch_id = "batch-denied-1"
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="eff-read",
                replay_class="safe",
                is_denied=False,
            ),
            DeliveryBatchItem(
                tool_index=1,
                tool_name="shell",
                ref="denied:shell:1",
                replay_class="unsafe",
                is_denied=True,
            ),
        )
        intent = DeliveryBatchIntent(
            batch_id=batch_id,
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            items=items,
            batch_digest=compute_batch_digest(items),
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent)
        b0 = self.store.load_batches(self.session_id, self.run_id)[0]
        self.assertFalse(b0.is_all_safe)
        self.assertFalse(b0.can_recover_before_provider_send)
        self.assertEqual(len(self.store.undelivered_replayable_batches(self.session_id, self.run_id)), 0)

    def test_compaction_retains_delivery_for_open_and_recovered_for_closed(self) -> None:
        batch_id = "batch-rec-1"
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="eff-1",
                replay_class="safe",
                is_denied=False,
            ),
        )
        intent = DeliveryBatchIntent(
            batch_id=batch_id,
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            items=items,
            batch_digest=compute_batch_digest(items),
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent)
        self.store.record_recovered(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            recovered_effect_ids=("eff-1",),
            recovered_reads=1,
            recovered_lookups=0,
        )

        # Before settlement (open op) -> compact retains both intent and recovered
        from codey.runtime.session_log import _compact_entries
        compacted_open = _compact_entries(self.log.read(self.session_id))
        open_delivery_kinds = [
            e.payload.get("record_kind")
            for e in compacted_open
            if e.kind == "operation_effect"
            and e.payload.get("effect_kind") == "tool_result_delivery"
        ]
        self.assertIn("batch_intent", open_delivery_kinds)
        self.assertIn("recovered", open_delivery_kinds)

        # Settle operation
        from codey.runtime.effects import mark_terminal
        committed = self.operations.commit(
            self.session_id,
            self.run_id,
            lambda st: mark_terminal(
                st,
                stop_reason="done",
                summary_chars=10,
                turns=1,
                max_turns=10,
                provider="mock_provider",
            ),
        )
        self.assertIsNotNone(committed)
        # After settlement -> compact keeps recovered fact, drops batch_intent
        all_entries = self.log.read(self.session_id)
        compacted_closed = _compact_entries(all_entries)
        closed_delivery_kinds = [
            e.payload.get("record_kind")
            for e in compacted_closed
            if e.kind == "operation_effect"
            and e.payload.get("effect_kind") == "tool_result_delivery"
        ]
        self.assertNotIn("batch_intent", closed_delivery_kinds)
        self.assertIn("recovered", closed_delivery_kinds)

        # Verify load_recovered_facts works even after compaction
        facts = self.store.load_recovered_facts(self.session_id, self.run_id)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].batch_id, batch_id)
        self.assertEqual(facts[0].recovered_reads, 1)

    def test_run_boundary_payload_lane_match_triggers_validation(self) -> None:
        # A corrupted entry with mismatched payload lane MUST trigger run boundary check and fail closed!
        expected_lane = lane_for_run(self.run_id)
        expected_op = operation_id_for_run(self.run_id)
        self.log.append(
            session_id=self.session_id,
            lane=expected_lane,
            operation_id=expected_op,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "tool_result_delivery",
                "record_kind": "batch_intent",
                "ref": "delivery_intent:b-mismatch",
                "batch_id": "b-mismatch",
                "session_id": self.session_id,
                "run_id": self.run_id,
                "lane": "wrong_payload_lane",  # Payload lane mismatch!
                "operation_id": expected_op,
                "turn": 1,
                "items": [{"tool_index": 0, "tool_name": "read", "ref": "eff-1", "replay_class": "safe", "is_denied": False}],
                "batch_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        with self.assertRaises(ToolResultDeliveryError) as ctx:
            self.store.load_batches(self.session_id, self.run_id)
        self.assertIn("run boundary mismatch", str(ctx.exception))

    def test_record_recovered_fails_closed_when_existing_recovered_entry_is_malformed(self) -> None:
        expected_op = operation_id_for_run(self.run_id)
        expected_lane = lane_for_run(self.run_id)
        # Inject malformed recovered entry with missing required fields
        self.log.append(
            session_id=self.session_id,
            lane=expected_lane,
            operation_id=expected_op,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "tool_result_delivery",
                "record_kind": "recovered",
                "ref": "delivery_recovered:b1",
                # missing batch_id, session_id, etc.
            },
        )
        with self.assertRaises(ToolResultDeliveryError):
            self.store.record_recovered(
                self.session_id,
                self.run_id,
                batch_id="b1",
                recovered_effect_ids=["eff-1"],
                recovered_reads=1,
                recovered_lookups=0,
            )


class SafeReplayRecoveryDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.session_id = "sess-recovery-deliv-1"
        self.run_id = "run-recovery-deliv-1"
        self.log = RuntimeSessionLog(self.project_dir / "state")
        self.operations = RuntimeOperationStore(self.log)
        self.effects = RuntimeEffectStore(self.log)
        self.delivery = ToolResultDeliveryStore(self.log)
        self.operations.start(
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            provider_id="mock_provider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )
        # Create test file
        (self.project_dir / "target.py").write_text("print('hello')\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_multi_safe_tools_one_settled_one_pending_reconstructs_full_batch(self) -> None:
        # Simulate turn 1: model called read and search
        # 1. read intent + settled
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=0,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": "target.py"},
            ),
        )
        self.effects.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.SAFE,
            ),
        )

        # 2. search intent (pending, crash before settlement)
        eff_search = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_search,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=1,
                tool_name="search",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": ".", "query": "hello"},
            ),
        )

        # 3. delivery batch intent was recorded
        batch_id = new_batch_id(self.run_id, 1)
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
            DeliveryBatchItem(
                tool_index=1,
                tool_name="search",
                ref=eff_search,
                replay_class="safe",
                is_denied=False,
            ),
        )
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        deps = Mock()
        deps.runtime_effects = self.effects
        deps.tool_result_delivery = self.delivery

        # Perform recovery
        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        self.assertEqual(len(recovery.recovered_tool_outcomes), 2)
        self.assertEqual(recovery.recovered_tool_result_batch_id, batch_id)

        # Verify outcomes
        r0 = recovery.recovered_tool_outcomes[0]
        r1 = recovery.recovered_tool_outcomes[1]
        self.assertEqual(r0.call.name, "read")
        self.assertEqual(r0.tool_index, 0)
        self.assertEqual(r0.effect_id, eff_read)
        self.assertTrue(r0.outcome.ok)
        self.assertIn("print('hello')", r0.outcome.model_text)

        self.assertEqual(r1.call.name, "search")
        self.assertEqual(r1.tool_index, 1)
        self.assertEqual(r1.effect_id, eff_search)
        self.assertTrue(r1.outcome.ok)

        # Verify delivery store has recovered fact
        batches = self.delivery.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)
        self.assertTrue(batches[0].is_recovered)
        self.assertEqual(batches[0].recovered_reads, 1)
        self.assertEqual(batches[0].recovered_lookups, 1)

    def test_run_details_merges_delivery_reads_and_runtime_lookups(self) -> None:
        # read settled (fact only in delivery) + search pending (replay in runtime effects)
        eff_search = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_search,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=1,
                tool_name="search",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": ".", "query": "hello"},
            ),
        )
        self.effects.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=eff_search,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.SAFE,
                replay_count=1,
                replayed_from_effect_id=eff_search,
            ),
        )
        # Delivery store has recovered both read and lookup
        self.delivery.record_recovered(
            self.session_id,
            self.run_id,
            batch_id="batch-merged-details",
            recovered_effect_ids=("eff-read", eff_search),
            recovered_reads=1,
            recovered_lookups=1,
        )

        details = load_run_details(
            run_ledgers=None,
            run_traces=None,
            runtime_operations=self.operations,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
            session_id=self.session_id,
            run_id=self.run_id,
        )
        self.assertTrue(details.available)
        recovery_values = [r.value for r in details.rows if r.label == "Recovery"]
        # Both Read and Lookup MUST be displayed!
        self.assertIn("Read action was recovered", recovery_values)
        self.assertIn("Lookup action was recovered", recovery_values)

    def test_run_details_handles_delivery_store_errors_with_warning(self) -> None:
        # Patch load_recovered_facts to raise error
        with patch.object(self.delivery, "load_recovered_facts", side_effect=ToolResultDeliveryError("corrupted")):
            details = load_run_details(
                run_ledgers=None,
                run_traces=None,
                runtime_operations=self.operations,
                runtime_effects=self.effects,
                tool_result_delivery=self.delivery,
                session_id=self.session_id,
                run_id=self.run_id,
            )
            self.assertTrue(details.available)
            recovery_values = [r.value for r in details.rows if r.label == "Recovery"]
            self.assertIn("Recovery details unavailable (receipt log error)", recovery_values)

    def test_real_execute_turn_tools_marks_safe_items_and_recovers_all_on_crash(self) -> None:
        calls = [
            ToolCall(name="read", args={"path": "target.py"}),
            ToolCall(name="search", args={"path": ".", "query": "hello"}),
        ]
        provider = MockDeliveryProvider()
        codec = JsonToolCodec()
        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="read and search",
            codec=codec,
            tool_result_delivery=self.delivery,
        )
        session = AgentLoopSession(
            request=req,
            provider=provider,
            project=self.project_dir,
            user_task="read and search",
            codec=codec,
            max_turns=5,
            stagnant_turns=3,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=True,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            profile=profile_for_name("coding_writer"),
            tool_fns=DEFAULT_TOOL_FNS,
            trace_recorder=None,
            trace=Mock(),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=True,
            progress=LoopProgress(changed_files=set(), read_file_paths=set(), known_file_paths=set()),
            verification=LoopVerification(paths=set(), edit_epoch=0, successful_checks=[], attempts=[]),
            stagnation=LoopStagnation(seen_info=set()),
            project_instructions=[],
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
        )

        res = execute_turn_tools(session, calls, turn=1)
        self.assertFalse(res.stopped)
        self.assertEqual(len(res.turn_state.results), 2)
        # Fast path check: turn_state MUST have delivery_batch_id set!
        self.assertTrue(bool(res.turn_state.delivery_batch_id))
        self.assertTrue(bool(res.turn_state.delivery_batch_digest))

        # Batch intent was recorded early with real "safe" classes!
        batches = self.delivery.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)
        b0 = batches[0]
        self.assertEqual(b0.intent.items[0].replay_class, "safe")
        self.assertEqual(b0.intent.items[1].replay_class, "safe")
        self.assertTrue(b0.is_all_safe)
        self.assertTrue(b0.can_recover_before_provider_send)

        # Simulate crash before provider send -> recovery re-executes both safe tools
        deps = Mock()
        deps.runtime_effects = self.effects
        deps.tool_result_delivery = self.delivery
        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        self.assertEqual(len(recovery.recovered_tool_outcomes), 2)
        self.assertEqual(recovery.recovered_tool_outcomes[0].call.name, "read")
        self.assertEqual(recovery.recovered_tool_outcomes[1].call.name, "search")

    def test_send_attempt_failure_blocks_provider_send_and_fails_closed(self) -> None:
        calls = [ToolCall(name="read", args={"path": "target.py"})]
        provider = MockDeliveryProvider()
        codec = JsonToolCodec()
        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="read target",
            codec=codec,
            tool_result_delivery=self.delivery,
        )
        session = AgentLoopSession(
            request=req,
            provider=provider,
            project=self.project_dir,
            user_task="read target",
            codec=codec,
            max_turns=5,
            stagnant_turns=3,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=True,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            profile=profile_for_name("coding_writer"),
            tool_fns=DEFAULT_TOOL_FNS,
            trace_recorder=None,
            trace=Mock(),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=True,
            progress=LoopProgress(changed_files=set(), read_file_paths=set(), known_file_paths=set()),
            verification=LoopVerification(paths=set(), edit_epoch=0, successful_checks=[], attempts=[]),
            stagnation=LoopStagnation(seen_info=set()),
            project_instructions=[],
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
        )
        res = execute_turn_tools(session, calls, turn=1)

        # Inject failure into record_send_attempt
        with patch.object(self.delivery, "record_send_attempt", side_effect=RuntimeError("disk full")):
            with self.assertRaises(ToolResultDeliveryError):
                deliver_turn_results(session, res.turn_state, 1)

        # Provider send MUST NOT have been called!
        self.assertEqual(len(provider.prompts), 0)

        # Provider effect settlement must be recorded as settled error
        effects = self.effects.load_effects(self.session_id, self.run_id)
        send_effects = [e for e in effects if e.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND]
        self.assertEqual(len(send_effects), 1)
        self.assertTrue(send_effects[0].is_settled)
        self.assertEqual(send_effects[0].settlement.error_code, "delivery_attempt_failed")
        self.assertEqual(send_effects[0].settlement.sent_state, "settled")

    def test_ensure_result_batch_intent_rejects_empty_ref(self) -> None:
        provider = MockDeliveryProvider()
        codec = JsonToolCodec()
        session = AgentLoopSession(
            request=AgentRequest(provider=provider, project=self.project_dir, task="test", codec=codec),
            provider=provider,
            project=self.project_dir,
            user_task="test",
            codec=codec,
            max_turns=5,
            stagnant_turns=3,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=True,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            profile=profile_for_name("coding_writer"),
            tool_fns=DEFAULT_TOOL_FNS,
            trace_recorder=None,
            trace=Mock(),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=True,
            progress=LoopProgress(changed_files=set(), read_file_paths=set(), known_file_paths=set()),
            verification=LoopVerification(paths=set(), edit_epoch=0, successful_checks=[], attempts=[]),
            stagnation=LoopStagnation(seen_info=set()),
            project_instructions=[],
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
        )
        turn_state = TurnState(
            results=[ToolResult(call=ToolCall(name="read", args={"path": "target.py"}), model_text="ok")],
            delivery_items=[
                ToolResultDeliveryItem(
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    ref="",  # empty ref!
                )
            ],
        )
        with self.assertRaises(ToolResultDeliveryError):
            ensure_result_batch_intent(session, turn_state, 1)

    def test_ensure_result_batch_intent_rejects_digest_mismatch_for_same_turn(self) -> None:
        # Pre-record batch intent for turn 1 with tool "read"
        items_early = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref="ref-1",
                replay_class="safe",
                is_denied=False,
            ),
        )
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id="batch-early-1",
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items_early,
                batch_digest=compute_batch_digest(items_early),
            ),
        )

        # Now try delivering turn 1 with unexpected different tool "search"
        provider = MockDeliveryProvider()
        codec = JsonToolCodec()
        session = AgentLoopSession(
            request=AgentRequest(provider=provider, project=self.project_dir, task="test", codec=codec),
            provider=provider,
            project=self.project_dir,
            user_task="test",
            codec=codec,
            max_turns=5,
            stagnant_turns=3,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=True,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            profile=profile_for_name("coding_writer"),
            tool_fns=DEFAULT_TOOL_FNS,
            trace_recorder=None,
            trace=Mock(),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=True,
            progress=LoopProgress(changed_files=set(), read_file_paths=set(), known_file_paths=set()),
            verification=LoopVerification(paths=set(), edit_epoch=0, successful_checks=[], attempts=[]),
            stagnation=LoopStagnation(seen_info=set()),
            project_instructions=[],
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
        )
        turn_state = TurnState(
            results=[ToolResult(call=ToolCall(name="search", args={"query": "q"}), model_text="ok")],
            delivery_items=[
                ToolResultDeliveryItem(
                    turn=1,
                    tool_index=0,
                    tool_name="search",
                    ref="ref-diff",
                    replay_class="safe",
                    is_denied=False,
                )
            ],
        )
        with self.assertRaises(ToolResultDeliveryError):
            ensure_result_batch_intent(session, turn_state, 1)

    def test_single_canonical_batch_for_read_plus_policy_denied_shell(self) -> None:
        calls = [
            ToolCall(name="read", args={"path": "target.py"}),
            ToolCall(name="shell", args={"command": "rm -rf /"}),
        ]
        provider = MockDeliveryProvider()
        codec = JsonToolCodec()
        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="read and shell",
            codec=codec,
            tool_result_delivery=self.delivery,
        )
        session = AgentLoopSession(
            request=req,
            provider=provider,
            project=self.project_dir,
            user_task="read and shell",
            codec=codec,
            max_turns=5,
            stagnant_turns=3,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=True,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            # shell is forbidden without approval channel -> policy denied
            profile=profile_for_name("planning_readonly"),
            tool_fns=DEFAULT_TOOL_FNS,
            trace_recorder=None,
            trace=Mock(),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=True,
            progress=LoopProgress(changed_files=set(), read_file_paths=set(), known_file_paths=set()),
            verification=LoopVerification(paths=set(), edit_epoch=0, successful_checks=[], attempts=[]),
            stagnation=LoopStagnation(seen_info=set()),
            project_instructions=[],
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
        )

        res = execute_turn_tools(session, calls, turn=1)
        self.assertFalse(res.stopped)
        self.assertEqual(len(res.turn_state.results), 2)

        # Deliver results to provider
        deliver_turn_results(session, res.turn_state, 1)

        # Exactly 1 batch must exist, and it must be delivered
        batches = self.delivery.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)
        self.assertTrue(batches[0].is_delivered)
        self.assertEqual(len(batches[0].send_attempts), 1)

    def test_policy_denied_tool_in_batch_fails_closed_in_recovery(self) -> None:
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=0,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": "target.py"},
            ),
        )
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
            DeliveryBatchItem(
                tool_index=1,
                tool_name="shell",
                ref="denied:shell:1",
                replay_class="unsafe",
                is_denied=True,
            ),
        )
        batch_id = "batch-denied-fail-closed"
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        deps = Mock()
        deps.runtime_effects = self.effects
        deps.tool_result_delivery = self.delivery

        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        # Must fail-closed: 0 recovered outcomes, read is blocked from single fallback
        self.assertEqual(len(recovery.recovered_tool_outcomes), 0)

    def test_send_attempt_without_delivered_fails_closed_in_recovery(self) -> None:
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=0,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": "target.py"},
            ),
        )
        batch_id = "batch-attempted-fail-closed"
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
        )
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        self.delivery.record_send_attempt(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            provider_effect_id="provider-attempt-1",
        )

        deps = Mock()
        deps.runtime_effects = self.effects
        deps.tool_result_delivery = self.delivery

        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        self.assertEqual(len(recovery.recovered_tool_outcomes), 0)
        pending = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 0)

    def test_mixed_batch_blocks_pending_safe_effect_from_single_replay(self) -> None:
        eff_edit = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)

        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_edit,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=0,
                tool_name="edit",
                replay_class=ReplayClass.UNSAFE,
            ),
        )
        self.effects.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=eff_edit,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.UNSAFE,
            ),
        )
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=1,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": "target.py"},
            ),
        )

        batch_id = "batch-mixed-fail-closed"
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="edit",
                ref=eff_edit,
                replay_class="unsafe",
                is_denied=False,
            ),
            DeliveryBatchItem(
                tool_index=1,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
        )
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        deps = Mock()
        deps.runtime_effects = self.effects
        deps.tool_result_delivery = self.delivery

        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        self.assertEqual(len(recovery.recovered_tool_outcomes), 0)
        pending = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 0)

    def test_delivered_batch_is_not_replayed(self) -> None:
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.effects.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                turn=1,
                tool_index=0,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
                replay_args={"path": "target.py"},
            ),
        )
        self.effects.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=eff_read,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.SAFE,
            ),
        )
        batch_id = "batch-delivered-ok"
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
        )
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        self.delivery.record_delivered(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            provider_effect_id="provider-eff-ok",
        )

        deps = Mock()
        deps.runtime_effects = self.effects
        deps.tool_result_delivery = self.delivery

        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        self.assertEqual(len(recovery.recovered_tool_outcomes), 0)


class AgentPromptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        (self.project_dir / "sample.txt").write_text("sample content", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clean_path_prompt_exact_byte_parity(self) -> None:
        codec = JsonToolCodec()
        call = ToolCall(name="read", args={"path": "sample.txt"})
        result = ToolResult(call=call, model_text="sample content")

        turn_state = TurnState(
            results=[result],
            delivery_items=[
                ToolResultDeliveryItem(
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    ref="eff-1",
                    effect_id="eff-1",
                    replay_class="safe",
                    is_denied=False,
                )
            ],
        )

        provider = MockDeliveryProvider()
        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="read sample file",
            codec=codec,
        )
        session = AgentLoopSession(
            request=req,
            provider=provider,
            project=self.project_dir,
            user_task="read sample file",
            codec=codec,
            max_turns=5,
            stagnant_turns=3,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=True,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            profile=profile_for_name("coding_writer"),
            tool_fns=DEFAULT_TOOL_FNS,
            trace_recorder=None,
            trace=Mock(),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=True,
            progress=LoopProgress(changed_files=set(), read_file_paths=set(), known_file_paths=set()),
            verification=LoopVerification(paths=set(), edit_epoch=0, successful_checks=[], attempts=[]),
            stagnation=LoopStagnation(seen_info=set()),
            project_instructions=[],
        )

        # 1. Computed via deliver_turn_results prompt builder
        actual_prompt = build_next_tool_prompt(session, turn_state, protocol_reminder="\n\nNote: reminder")

        # 2. Canonical expected prompt via exact sequential concatenation
        raw_results = codec.format_results(turn_state.results)
        expected_uncontext = f"{raw_results}\n\nNote: reminder"
        expected_prompt = append_coding_context(session, expected_uncontext)

        # Must be 100% exact byte-for-byte equal!
        self.assertEqual(actual_prompt, expected_prompt)


if __name__ == "__main__":
    unittest.main()
