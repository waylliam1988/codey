"""Deterministic tests for agent effect sandwich (intent -> real effect -> settlement)."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, Mock, patch

from codey.agents.prompt_context import _send_provider_with_effect
from codey.agents.request import AgentRequest
from codey.agents.state import AgentLoopSession, LoopProgress, LoopStagnation, LoopVerification, RunResult
from codey.agents.tool_execution import (
    TurnState,
    emit_tool_started_after_intent,
    evaluate_tool_call_policy,
    execute_tool_call,
    policy_denied,
    record_tool_call_intent,
    record_tool_call_settlement,
    record_tool_outcome,
)
from codey.agents.tools import AgentToolFns
from codey.app import server
from codey.operations.task_entry import run_task_submission
from codey.operations.task_run import TaskRunDeps, _settle_pending_effects_for_resume, _start_run_operation
from codey.policies.permissions import profile_for_name
from codey.protocols import JsonToolCodec
from codey.runtime.effect_records import (
    RuntimeEffectStore,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.models import ToolCall
from codey.runtime.prompt_envelope import FailOpenPromptTrace
from codey.runtime.session_log import RuntimeSessionLog
from codey.task.model import TaskSubmission
from codey.toolchain.runtime import ToolOutcome


class MockProvider:
    def __init__(self, reply: str = "mock reply", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.send_history: list[str] = []

    @property
    def name(self) -> str:
        return "mock_provider"

    def send(self, prompt: str) -> str:
        self.send_history.append(prompt)
        if self.fail:
            raise RuntimeError("provider communication error")
        return self.reply


class AgentEffectSandwichTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name)
        self.session_id = "sess-sandwich-1"
        self.run_id = "run-sandwich-1"
        self.log = RuntimeSessionLog(self.project_dir / "state")
        self.operations = RuntimeOperationStore(self.log)
        self.effects = RuntimeEffectStore(self.log)
        self.operations.start(
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            provider_id="mock_provider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_session(self, provider: MockProvider) -> AgentLoopSession:
        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="do something",
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
        )
        profile = profile_for_name("coding_writer")
        return AgentLoopSession(
            request=req,
            provider=provider,
            project=self.project_dir,
            user_task="do something",
            codec=JsonToolCodec(permission_profile=profile.name),
            max_turns=10,
            stagnant_turns=4,
            on_event=lambda e: None,
            on_shell_request=None,
            stop_flag=None,
            fresh_chat=False,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=None,
            active_provider_id="mock_provider",
            handoff="",
            project_facts="",
            research_context="",
            project_map="",
            project_config_warnings="",
            work_checkpoint="",
            verification_candidates=(),
            verification_candidate_loader=None,
            coding_context_enabled=False,
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context="",
            completion_repair_context_payload=None,
            profile=profile,
            tool_fns=AgentToolFns(
                read_file=lambda *a, **kw: ToolOutcome("file text", True),
                edit_file=lambda *a, **kw: ToolOutcome("edited", True, changed=True),
                write_file=lambda *a, **kw: ToolOutcome("written", True, changed=True),
                list_directory=lambda *a, **kw: ToolOutcome("dir listing", True),
                search_files=lambda *a, **kw: ToolOutcome("search results", True),
                find_references=lambda *a, **kw: ToolOutcome("refs", True),
                run_command=lambda *a, **kw: ToolOutcome("command output", True),
            ),
            trace_recorder=None,
            trace=FailOpenPromptTrace(None),
            system_prompt_text="",
            project_text=str(self.project_dir),
            verification_required=False,
            verification_forbidden=False,
            progress=LoopProgress(set(), set(), set()),
            verification=LoopVerification(set(), 0, [], []),
            stagnation=LoopStagnation(set()),
            project_instructions=[],
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
        )

    def test_provider_send_intent_and_settlement_on_success(self) -> None:
        provider = MockProvider("hello model")
        session = self._create_session(provider)

        reply = _send_provider_with_effect(
            session,
            "user prompt",
            purpose="test provider send",
            source_ref="provider_send:test",
        )
        self.assertEqual(reply, "hello model")

        effects = self.effects.load_effects(self.session_id, self.run_id)
        self.assertEqual(len(effects), 1)
        proj = effects[0]
        self.assertEqual(proj.intent.effect_category, "provider_send")
        self.assertTrue(proj.is_settled)
        self.assertEqual(proj.settlement.status, SETTLEMENT_STATUS_OK)
        self.assertEqual(proj.settlement.sent_state, "settled")

    def test_provider_send_intent_and_settlement_on_error(self) -> None:
        provider = MockProvider(fail=True)
        session = self._create_session(provider)

        with self.assertRaises(RuntimeError):
            _send_provider_with_effect(
                session,
                "user prompt",
                purpose="test provider send error",
                source_ref="provider_send:test",
            )

        effects = self.effects.load_effects(self.session_id, self.run_id)
        self.assertEqual(len(effects), 1)
        proj = effects[0]
        self.assertTrue(proj.is_settled)
        self.assertEqual(proj.settlement.status, SETTLEMENT_STATUS_ERROR)
        self.assertEqual(proj.settlement.sent_state, "maybe_sent")

    def test_tool_call_effect_sandwich_sequence(self) -> None:
        provider = MockProvider()
        session = self._create_session(provider)
        events: list[str] = []
        session.on_event = lambda e: events.append(e.kind)

        call = ToolCall(name="read", args={"path": "foo.py"})
        policy_decision, replay_decision = evaluate_tool_call_policy(
            session,
            call,
            turn=1,
            tool_index=0,
        )
        self.assertFalse(policy_denied(policy_decision))

        # 1. Record intent
        effect_id = record_tool_call_intent(
            session,
            call,
            turn=1,
            tool_index=0,
            replay_decision=replay_decision,
        )
        self.assertTrue(bool(effect_id))

        # Pending should now have 1 effect
        pending = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].intent.tool_name, "read")

        # 2. Emit tool started
        emit_tool_started_after_intent(session, call, turn=1, tool_index=0)
        self.assertIn("tool_start", events)

        # 3. Execute tool
        outcome = execute_tool_call(session, call, turn=1, tool_index=0)
        self.assertTrue(outcome.ok)

        # 4. Record tool outcome first
        turn_state = TurnState()
        record_tool_outcome(
            session,
            turn_state,
            turn=1,
            call=call,
            outcome=outcome,
            tool_index=0,
        )
        self.assertEqual(len(turn_state.results), 1)

        # 5. Settle after outcome
        record_tool_call_settlement(
            session,
            effect_id,
            outcome=outcome,
            replay_decision=replay_decision,
        )

        # Verify no pending effects
        self.assertEqual(len(self.effects.pending_effects(self.session_id, self.run_id)), 0)
        settled_effects = self.effects.load_effects(self.session_id, self.run_id)
        self.assertEqual(settled_effects[0].settlement.status, SETTLEMENT_STATUS_OK)

    def test_resume_recovery_failure_fails_closed(self) -> None:
        # If pending effects cannot be loaded (corrupted log), _settle_pending_effects_for_resume returns False
        broken_store = MagicMock()
        broken_store.pending_effects.side_effect = RuntimeError("disk corrupt")

        deps = MagicMock()
        deps.runtime_effects = broken_store
        result = _settle_pending_effects_for_resume(deps, session_id="s1", run_id="r1")
        self.assertFalse(result)

    def test_record_intent_failure_in_loop_does_not_execute_tool_or_settle(self) -> None:
        from codey.agents.loop import _run_loop

        executed_tools: list[str] = []
        provider = MockProvider()
        session = self._create_session(provider)
        custom_tools = AgentToolFns(
            read_file=lambda *a, **kw: executed_tools.append("read") or ToolOutcome("ok", True),
            edit_file=session.tool_fns.edit_file,
            write_file=session.tool_fns.write_file,
            list_directory=session.tool_fns.list_directory,
            search_files=session.tool_fns.search_files,
            find_references=session.tool_fns.find_references,
            run_command=session.tool_fns.run_command,
        )
        object.__setattr__(session, "tool_fns", custom_tools)

        reply_json = '```json\n{"calls": [{"name": "read", "args": {"path": "foo.py"}}], "control": {"action": "done", "body": "finished"}}\n```'
        with patch("codey.agents.loop.record_tool_call_intent", side_effect=RuntimeError("intent write failed")):
            _run_loop(session, reply_json)

        # Tool should NOT have been executed
        self.assertEqual(len(executed_tools), 0)
        # No settlements should have been recorded
        effects = self.effects.load_effects(self.session_id, self.run_id)
        tool_settlements = [p for p in effects if p.intent.effect_category == "tool_call" and p.is_settled]
        self.assertEqual(len(tool_settlements), 0)

    def test_start_run_operation_fails_closed_when_recovery_fails(self) -> None:
        broken_store = MagicMock()
        broken_store.pending_effects.side_effect = RuntimeError("disk corrupt")

        deps = MagicMock()
        deps.runtime_effects = broken_store
        deps.state.runtime_operations = self.operations
        work = MagicMock()

        ok = _start_run_operation(
            deps,
            work,
            session_id=self.session_id,
            run_id="run-recover-fail-1",
            project=str(self.project_dir),
            provider_id="mock",
            turn_budget=5,
            max_repair_rounds=1,
            task_kind="project",
        )
        self.assertFalse(ok)

    def test_execute_task_run_fails_closed_when_recovery_fails(self) -> None:
        state = server.AppContext(state_home=self.temp_dir.name)
        broken_effects = Mock()
        broken_effects.pending_effects.side_effect = RuntimeError("disk corrupt")

        agent_called = False

        def fake_agent_run(req: Any) -> RunResult:
            nonlocal agent_called
            agent_called = True
            return RunResult("ok", "done", 1, 0, (), ())

        deps = TaskRunDeps(
            state=state,
            agent_run=fake_agent_run,
            collect_changes=Mock(return_value={"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"}),
            run_review=Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            workspace_revisions=state.workspace_revisions,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            runtime_effects=broken_effects,
            is_git_repository=lambda _project: True,
        )

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch("codey.operations.task_run.run_ghost_post_turn", autospec=True) as mock_ghost:
            run_task_submission(
                deps,
                TaskSubmission(
                    self.session_id,
                    str(self.project_dir),
                    "task to run",
                    5,
                    False,
                    "mock_provider",
                    intent="project",
                    run_id="run-fail-closed-1",
                ),
            )

        # Agent / Provider should NEVER have been called
        self.assertFalse(agent_called)
        # Registry must NOT be busy
        self.assertFalse(state.run_registry.is_busy())
        # Not a provider failure
        self.assertIsNone(state.run_registry.last_provider_failure())
        # Ghost post-turn called cleanly
        mock_ghost.assert_called_once()
        # Terminal event must be stop_reason="error"
        done_events = [e for e in emitted_events if e.get("type") == "task_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0].get("stop_reason"), "error")

    def test_execute_task_run_fails_closed_when_operation_start_returns_none(self) -> None:
        state = server.AppContext(state_home=self.temp_dir.name)
        agent_called = False

        def fake_agent_run(req: Any) -> RunResult:
            nonlocal agent_called
            agent_called = True
            return RunResult("ok", "done", 1, 0, (), ())

        deps = TaskRunDeps(
            state=state,
            agent_run=fake_agent_run,
            collect_changes=Mock(return_value={"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"}),
            run_review=Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            workspace_revisions=state.workspace_revisions,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            runtime_effects=self.effects,
            is_git_repository=lambda _project: True,
        )

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch.object(state.runtime_operations, "start", return_value=None):
            run_task_submission(
                deps,
                TaskSubmission(
                    self.session_id,
                    str(self.project_dir),
                    "task to run",
                    5,
                    False,
                    "mock_provider",
                    intent="project",
                    run_id="run-op-none-1",
                ),
            )

        # Agent / Provider should NEVER have been called
        self.assertFalse(agent_called)
        # Registry must NOT be busy
        self.assertFalse(state.run_registry.is_busy())
        # Terminal event must be stop_reason="error"
        done_events = [e for e in emitted_events if e.get("type") == "task_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0].get("stop_reason"), "error")

    def test_recovery_failure_single_work_item_transition(self) -> None:
        from codey.ghost.work_queue import GhostWorkClaimResult, GhostWorkItem

        state = server.AppContext(state_home=self.temp_dir.name)
        mock_work_queue = Mock()
        state.ghost_work_queue = mock_work_queue

        broken_effects = Mock()
        broken_effects.pending_effects.side_effect = RuntimeError("disk corrupt")

        claimed_work_item = GhostWorkItem(
            id="item-123",
            kind="project",
            status="running",
            scope="project",
            scope_ref="",
            title="test task",
            why_now="recovery",
            priority=1.0,
            confidence=1.0,
            source="manual",
            source_ref="",
            started_run_id="run-transition-1",
        )
        claim_result = GhostWorkClaimResult(
            ok=True,
            item=claimed_work_item,
            mode="project",
            task="task to run",
        )
        deps = TaskRunDeps(
            state=state,
            agent_run=Mock(),
            collect_changes=Mock(return_value={"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"}),
            run_review=Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            workspace_revisions=state.workspace_revisions,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            runtime_effects=broken_effects,
            is_git_repository=lambda _project: True,
        )

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch("codey.operations.task_run.maybe_claim_work_item", return_value=claim_result):
            run_task_submission(
                deps,
                TaskSubmission(
                    self.session_id,
                    str(self.project_dir),
                    "task to run",
                    5,
                    False,
                    "mock_provider",
                    intent="project",
                    run_id="run-transition-1",
                ),
            )

        # Recovery must have been attempted and failed
        broken_effects.pending_effects.assert_called_once()
        # Should block the item on error, not call release_item
        mock_work_queue.block_item.assert_called_once_with("item-123", run_id="run-transition-1", blocked_reason="error")
        mock_work_queue.release_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
