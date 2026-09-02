"""Deterministic tests for tool result delivery tracking, receipts, and projection (0.5.5)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from codey.agents.request import AgentRequest
from codey.agents.result_delivery import build_next_tool_prompt
from codey.agents.state import AgentLoopSession, LoopProgress, LoopStagnation, LoopVerification
from codey.agents.tool_execution import (
    ToolResultDeliveryItem,
    TurnState,
)
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.operations.recovery import recover_effects_for_resume
from codey.policies.permissions import profile_for_name
from codey.protocols import JsonToolCodec
from codey.runtime.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    SETTLEMENT_STATUS_OK,
    new_effect_id,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.models import ToolCall, ToolResult
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
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
        # Forbidden raw keys in intent payload
        intent = DeliveryBatchIntent(
            batch_id="batch-1",
            session_id=self.session_id,
            run_id=self.run_id,
            lane="lane-1",
            operation_id="op-1",
            turn=1,
            tool_refs=("ref1",),
            tool_names=("read",),
            batch_digest="d" * 16,
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
                tool_refs=(),
                tool_names=(),
                batch_digest="d",
            ).validate()

        # Try validating payload with raw result key
        raw_payload = intent.to_payload()
        raw_payload["raw_result"] = "some secret code text"
        with self.assertRaises(ToolResultDeliveryError):
            from codey.runtime.tool_result_delivery import _check_no_forbidden_keys
            _check_no_forbidden_keys(raw_payload)

    def test_batch_lifecycle_intent_attempt_delivered(self) -> None:
        batch_id = "batch-life-1"
        tool_refs = ("eff-1", "eff-2")
        tool_names = ("read", "search")
        digest = compute_batch_digest(tool_refs, tool_names)

        intent = DeliveryBatchIntent(
            batch_id=batch_id,
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            tool_refs=tool_refs,
            tool_names=tool_names,
            batch_digest=digest,
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent)

        batches = self.store.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)
        b0 = batches[0]
        self.assertEqual(b0.intent.batch_id, batch_id)
        self.assertFalse(b0.is_delivered)
        self.assertTrue(b0.is_all_safe)
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

    def test_unsafe_batch_not_in_undelivered_replayable(self) -> None:
        batch_id = "batch-unsafe-1"
        intent = DeliveryBatchIntent(
            batch_id=batch_id,
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            tool_refs=("eff-read", "eff-edit"),
            tool_names=("read", "edit"),
            batch_digest=compute_batch_digest(("eff-read", "eff-edit"), ("read", "edit")),
        )
        self.store.record_batch_intent(self.session_id, self.run_id, intent)
        b0 = self.store.load_batches(self.session_id, self.run_id)[0]
        self.assertFalse(b0.is_all_safe)
        self.assertEqual(len(self.store.undelivered_replayable_batches(self.session_id, self.run_id)), 0)

    def test_compaction_retains_delivery_for_open_and_recovered_for_closed(self) -> None:
        batch_id = "batch-rec-1"
        intent = DeliveryBatchIntent(
            batch_id=batch_id,
            session_id=self.session_id,
            run_id=self.run_id,
            turn=1,
            tool_refs=("eff-1",),
            tool_names=("read",),
            batch_digest=compute_batch_digest(("eff-1",), ("read",)),
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
        tool_refs = (eff_read, eff_search)
        tool_names = ("read", "search")
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_refs=tool_refs,
                tool_names=tool_names,
                batch_digest=compute_batch_digest(tool_refs, tool_names),
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

    def test_delivered_batch_is_not_replayed(self) -> None:
        # Same setup, but batch was delivered before crash
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
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_refs=(eff_read,),
                tool_names=("read",),
                batch_digest=compute_batch_digest((eff_read,), ("read",)),
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

    def test_mixed_safe_and_unsafe_batch_fails_closed(self) -> None:
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        eff_edit = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)

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
                tool_index=1,
                tool_name="edit",
                replay_class=ReplayClass.UNSAFE,
            ),
        )

        batch_id = "batch-mixed"
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_refs=(eff_read, eff_edit),
                tool_names=("read", "edit"),
                batch_digest=compute_batch_digest((eff_read, eff_edit), ("read", "edit")),
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
        # Mixed batch is not replayed
        self.assertEqual(len(recovery.recovered_tool_outcomes), 0)
        # Edit pending is synthesized to interrupted
        pending_after = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending_after), 0)


class AgentPromptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        (self.project_dir / "sample.txt").write_text("sample content", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clean_path_prompt_parity(self) -> None:
        # Verify that deliver_turn_results produces the exact same prompt structure as manual formatting
        codec = JsonToolCodec()
        call = ToolCall(name="read", args={"path": "sample.txt"})
        result = ToolResult(call=call, model_text="sample content")

        turn_state = TurnState(
            results=[result],
            delivery_items=[ToolResultDeliveryItem(turn=1, tool_index=0, tool_name="read")],
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

        prompt = build_next_tool_prompt(session, turn_state, protocol_reminder="\n\nNote: reminder")
        self.assertIn("tool=read_file", prompt)
        self.assertIn("sample content", prompt)
        self.assertIn("Note: reminder", prompt)


if __name__ == "__main__":
    unittest.main()
