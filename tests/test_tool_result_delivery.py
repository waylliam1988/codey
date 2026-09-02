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
from codey.agents.tool_turn import execute_turn_tools
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.operations.recovery import recover_effects_for_resume
from codey.policies.permissions import profile_for_name
from codey.protocols import JsonToolCodec
from codey.runs.details import load_run_details
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
        intent = DeliveryBatchIntent(
            batch_id="batch-1",
            session_id=self.session_id,
            run_id=self.run_id,
            lane="lane-1",
            operation_id="op-1",
            turn=1,
            tool_refs=("ref1",),
            tool_names=("read",),
            batch_digest=compute_batch_digest(("ref1",), ("read",)),
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

    def test_schema_hygiene_strict_checks(self) -> None:
        # Mismatched lengths
        with self.assertRaises(ToolResultDeliveryError):
            compute_batch_digest(("ref1", "ref2"), ("read",))

        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchIntent(
                batch_id="batch-1",
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_refs=("ref1", "ref2"),
                tool_names=("read",),
                batch_digest="digest",
            ).validate()

        # Mismatched digest
        with self.assertRaises(ToolResultDeliveryError):
            DeliveryBatchIntent(
                batch_id="batch-1",
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_refs=("ref1",),
                tool_names=("read",),
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
        self.assertFalse(b0.can_recover_before_provider_send)
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

        # Verify load_recovered_facts works even after compaction
        facts = self.store.load_recovered_facts(self.session_id, self.run_id)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].batch_id, batch_id)
        self.assertEqual(facts[0].recovered_reads, 1)


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

    def test_send_attempt_without_delivered_fails_closed_in_recovery(self) -> None:
        # Batch had send_attempt recorded before crash (provider may have already received prompt)
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
        # Must NOT recover any outcomes because provider send already started!
        self.assertEqual(len(recovery.recovered_tool_outcomes), 0)
        # Pending read must be synthesized to interrupted, not replayed
        pending = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 0)

    def test_mixed_batch_blocks_pending_safe_effect_from_single_replay(self) -> None:
        # edit settled + read pending + mixed batch intent
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
        self.delivery.record_batch_intent(
            self.session_id,
            self.run_id,
            DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_refs=(eff_edit, eff_read),
                tool_names=("edit", "read"),
                batch_digest=compute_batch_digest((eff_edit, eff_read), ("edit", "read")),
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
        # Even though read is pending and safe, it MUST NOT be replayed because the batch was mixed!
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

    def test_turn_tool_execution_natural_crash_window(self) -> None:
        # Test execute_turn_tools records batch_intent BEFORE executing any tool
        calls = [
            ToolCall(name="read", args={"path": "target.py"}),
            ToolCall(name="search", args={"query": "hello"}),
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

        # Batch intent was recorded early
        batches = self.delivery.load_batches(self.session_id, self.run_id)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].intent.tool_names, ("read", "search"))

    def test_load_recovered_facts_and_run_details_consumption(self) -> None:
        batch_id = "batch-details-1"
        self.delivery.record_recovered(
            self.session_id,
            self.run_id,
            batch_id=batch_id,
            recovered_effect_ids=("eff-1",),
            recovered_reads=1,
            recovered_lookups=0,
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
        recovery_rows = [r for r in details.rows if r.label == "Recovery"]
        self.assertEqual(len(recovery_rows), 1)
        self.assertEqual(recovery_rows[0].value, "Read action was recovered")


class AgentPromptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        (self.project_dir / "sample.txt").write_text("sample content", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clean_path_prompt_parity(self) -> None:
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
