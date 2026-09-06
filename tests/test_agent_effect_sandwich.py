"""Deterministic tests for agent effect sandwich (intent -> real effect -> settlement)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import MagicMock, Mock, patch

from codey.agents.prompt_context import _send_provider_with_effect
from codey.agents.request import AgentRequest
from codey.agents.state import AgentLoopSession, LoopProgress, LoopStagnation, LoopVerification, RunResult
from codey.agents.tool_execution import (
    TurnState,
    build_tool_call_intent,
    emit_tool_started_after_intent,
    evaluate_tool_call_policy,
    execute_tool_call,
    policy_denied,
    record_tool_outcome,
    settle_tool_call_effect,
)
from codey.agents.tools import AgentToolFns
from codey.app import server
from codey.operations.recovery import ResumeRecoveryResult, recover_effects_for_resume
from codey.operations.task_entry import run_task_submission
from codey.operations.task_run import TaskRunDeps, _start_run_operation
from codey.policies.permissions import profile_for_name
from codey.protocols import JsonToolCodec
from codey.runtime.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectStore,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    new_effect_id,
)
from codey.runtime import cancellation
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.operation_state import (
    RuntimeOperationStore,
    lane_for_run,
    mark_tool_effect_pending,
    operation_id_for_run,
)
from codey.runtime.models import ToolCall
from codey.runtime.prompt_envelope import FailOpenPromptTrace
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeLogEntry, RuntimeSessionLog
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
    new_batch_id,
)
from codey.task.model import TaskSubmission
from codey.toolchain.runtime import ToolOutcome


def _commit_log_entry(
    log: RuntimeSessionLog,
    session_id: str,
    *,
    lane: str,
    operation_id: str,
    kind: str,
    payload: dict[str, object],
) -> None:
    path = log.path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = RuntimeLogEntry(
        session_id=session_id,
        lane=lane,
        operation_id=operation_id,
        kind=kind,
        payload=payload,
    )
    with path.open("ab") as handle:
        handle.write(entry.to_json_line().encode("utf-8"))


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
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.project_dir = Path(self.temp_dir.name)
        self.session_id = "sess-sandwich-1"
        self.run_id = "run-sandwich-1"
        self.log = RuntimeSessionLog(self.project_dir / "state")
        self.operations = RuntimeOperationStore(self.log)
        self.effects = RuntimeEffectStore(self.log)
        self.delivery = ToolResultDeliveryStore(self.log)
        self.line = RuntimeMutationLine(self.log)
        self.line.accept_operation(
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            provider_id="mock_provider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )
        self.line.mark_writer_running(
            self.session_id,
            self.run_id,
            provider_id="mock_provider",
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
            runtime_mutations=self.line,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
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
            runtime_mutations=self.line,
            runtime_effects=self.effects,
            tool_result_delivery=self.delivery,
        )

    def _commit_tool_batch(
        self,
        intent: RuntimeEffectIntent,
        *,
        turn: int,
        tool_index: int,
        tool_name: str,
        replay_class: str,
    ) -> str:
        batch_id = new_batch_id(self.run_id, turn)
        items = (
            DeliveryBatchItem(
                tool_index=tool_index,
                tool_name=tool_name,
                ref=intent.effect_id,
                replay_class=replay_class,
                is_denied=False,
            ),
        )
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(intent,),
            delivery_intent=DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=turn,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        return batch_id

    def _deps(self) -> SimpleNamespace:
        return SimpleNamespace(
            runtime_effects=self.effects,
            runtime_mutations=self.line,
            tool_result_delivery=self.delivery,
            state=SimpleNamespace(
                runtime_effects=self.effects,
                runtime_mutations=self.line,
                tool_result_delivery=self.delivery,
            ),
        )

    def test_unknown_tool_is_policy_denied_and_recorded(self) -> None:
        decisions: list[object] = []

        class Trace:
            def record_policy_decision(self, decision: object) -> None:
                decisions.append(decision)

        session = self._create_session(MockProvider())
        session.trace = FailOpenPromptTrace(Trace())
        call = ToolCall("bash", {"command": "echo unsafe", "path": "."})

        policy_decision, replay_decision = evaluate_tool_call_policy(
            session,
            call,
            turn=1,
            tool_index=0,
        )

        self.assertIsNotNone(policy_decision)
        assert policy_decision is not None
        self.assertTrue(policy_denied(policy_decision))
        self.assertEqual(policy_decision.reason_code, "unknown_action")
        self.assertEqual(policy_decision.kind, "unknown_tool")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(getattr(replay_decision, "reason", ""), "policy_denied")

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

        # 1. Build intent, then commit it with the delivery envelope as one mutation.
        intent = build_tool_call_intent(
            session,
            call,
            turn=1,
            tool_index=0,
            replay_decision=replay_decision,
        )
        assert intent is not None
        effect_id = intent.effect_id
        self._commit_tool_batch(
            intent,
            turn=1,
            tool_index=0,
            tool_name="read",
            replay_class="safe",
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
        settle_tool_call_effect(
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
        # If reducer-selected recovery needs the effect ledger and it cannot load,
        # recovery fails closed.
        session = self._create_session(MockProvider())
        call = ToolCall(name="read", args={"path": "foo.py"})
        _, replay_decision = evaluate_tool_call_policy(session, call, turn=1, tool_index=0)
        intent = build_tool_call_intent(
            session,
            call,
            turn=1,
            tool_index=0,
            replay_decision=replay_decision,
        )
        assert intent is not None
        self._commit_tool_batch(
            intent,
            turn=1,
            tool_index=0,
            tool_name="read",
            replay_class="safe",
        )
        broken_store = MagicMock()
        broken_store.load_effects.side_effect = RuntimeError("disk corrupt")

        deps = MagicMock()
        deps.runtime_effects = broken_store
        deps.runtime_mutations = self.line
        recovery = recover_effects_for_resume(
            deps,
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertFalse(recovery.ok)
        self.assertEqual(recovery.recovered_tool_outcomes, ())

    def test_tool_batch_commit_failure_in_loop_does_not_execute_tool_or_settle(self) -> None:
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

        reply_json = '{"tool": "read", "args": {"path": "foo.py"}}'
        with patch.object(
            session.runtime_mutations,
            "begin_tool_batch",
            side_effect=RuntimeError("intent write failed"),
        ):
            with self.assertRaises(RuntimeError):
                _run_loop(session, reply_json)

        # Tool should NOT have been executed
        self.assertEqual(len(executed_tools), 0)
        # No settlements should have been recorded
        effects = self.effects.load_effects(self.session_id, self.run_id)
        tool_settlements = [p for p in effects if p.intent.effect_category == "tool_call" and p.is_settled]
        self.assertEqual(len(tool_settlements), 0)

    def test_start_run_operation_only_starts_operation(self) -> None:
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
        self.assertTrue(ok)
        broken_store.pending_effects.assert_not_called()
        self.assertIsNotNone(work.operation)

    def test_execute_task_run_fails_closed_when_recovery_fails(self) -> None:
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
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
            is_git_repository=lambda _project: True,
        )

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch(
                 "codey.operations.task_run.recover_effects_for_resume",
                 return_value=ResumeRecoveryResult(ok=False),
             ), \
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
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
            is_git_repository=lambda _project: True,
        )

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch.object(state.runtime_mutations, "accept_operation", return_value=None):
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

    def test_runtime_start_failure_happens_before_work_item_claim(self) -> None:
        state = server.AppContext(state_home=self.temp_dir.name)
        mock_work_queue = Mock()
        state.ghost_work_queue = mock_work_queue

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
            runtime_effects=self.effects,
            is_git_repository=lambda _project: True,
        )

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch("codey.operations.task_run.maybe_claim_work_item") as claim_work_item, \
             patch.object(state.runtime_mutations, "accept_operation", return_value=None):
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

        claim_work_item.assert_not_called()
        mock_work_queue.block_item.assert_not_called()
        mock_work_queue.release_item.assert_not_called()

    def test_recovery_fails_before_ghost_router_provider_send(self) -> None:
        state = server.AppContext(state_home=self.temp_dir.name)
        router_provider = Mock()
        router_provider.send = Mock(return_value=Mock(content="auto route output", tool_calls=[]))
        router_factory = Mock(return_value=router_provider)

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
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
            ghost_router_provider_factory=router_factory,
            is_git_repository=lambda _project: True,
        )

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)

        with patch("codey.operations.ghost_post_turn._ghost_learning_enabled", return_value=True), \
             patch(
                 "codey.operations.task_run.recover_effects_for_resume",
                 return_value=ResumeRecoveryResult(ok=False),
             ):
            run_task_submission(
                deps,
                TaskSubmission(
                    self.session_id,
                    str(self.project_dir),
                    "task to run",
                    5,
                    False,
                    "mock_provider",
                    intent="auto",
                    run_id="run-auto-router-gate-1",
                ),
            )

        # Recovery failed before router/provider dispatch.
        router_factory.assert_not_called()
        router_provider.send.assert_not_called()
        # Registry must NOT be busy
        self.assertFalse(state.run_registry.is_busy())
        # Terminal event must be stop_reason="error"
        done_events = [e for e in emitted_events if e.get("type") == "task_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0].get("stop_reason"), "error")

    def test_recovered_tool_outcomes_skip_work_claim_and_auto_router(self) -> None:
        state = server.AppContext()
        run_id = "run-recovered-skip-router-1"
        target = self.project_dir / "target.txt"
        target.write_text("recovered file text", encoding="utf-8")

        self.assertIsNotNone(
            state.runtime_mutations.accept_operation(
                session_id=self.session_id,
                run_id=run_id,
                project=str(self.project_dir),
                provider_id="mock_provider",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
        )
        state.runtime_mutations.mark_writer_running(
            self.session_id,
            run_id,
            provider_id="mock_provider",
        )
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, run_id)
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=effect_id,
                replay_class="safe",
                is_denied=False,
            ),
        )
        state.runtime_mutations.begin_tool_batch(
            self.session_id,
            run_id,
            intents=(
                RuntimeEffectIntent(
                    effect_id=effect_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id=self.session_id,
                    run_id=run_id,
                    phase="writer",
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    replay_class=ReplayClass.SAFE,
                    replay_args={"path": "target.txt"},
                ),
            ),
            delivery_intent=DeliveryBatchIntent(
                batch_id=new_batch_id(run_id, 1),
                session_id=self.session_id,
                run_id=run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        seen_requests: list[Any] = []

        def fake_agent_run(req: Any) -> RunResult:
            seen_requests.append(req)
            return RunResult("ok", "done", 2, False, False, False)

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
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
            is_git_repository=lambda _project: True,
        )

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch("codey.operations.task_run.maybe_claim_work_item", autospec=True) as mock_claim, \
             patch("codey.operations.task_run.maybe_route_auto", autospec=True) as mock_route:
            run_task_submission(
                deps,
                TaskSubmission(
                    self.session_id,
                    str(self.project_dir),
                    "continue task",
                    5,
                    False,
                    "mock_provider",
                    intent="auto",
                    run_id=run_id,
                ),
            )

        mock_claim.assert_not_called()
        mock_route.assert_not_called()
        self.assertEqual(len(seen_requests), 1)
        recovered = seen_requests[0].recovered_tool_outcomes
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].call.name, "read")
        self.assertIn("recovered file text", recovered[0].outcome.model_text)

    def test_recovered_hybrid_writer_resume_dispatches_project_not_research(self) -> None:
        state = server.AppContext()
        run_id = "run-recovered-hybrid-writer-1"
        target = self.project_dir / "target.txt"
        target.write_text("hybrid recovered file text", encoding="utf-8")

        self.assertIsNotNone(
            state.runtime_mutations.accept_operation(
                session_id=self.session_id,
                run_id=run_id,
                project=str(self.project_dir),
                provider_id="mock_provider",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="hybrid",
            )
        )
        state.runtime_mutations.mark_writer_running(
            self.session_id,
            run_id,
            provider_id="mock_provider",
        )
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, run_id)
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=effect_id,
                replay_class="safe",
                is_denied=False,
            ),
        )
        state.runtime_mutations.begin_tool_batch(
            self.session_id,
            run_id,
            intents=(
                RuntimeEffectIntent(
                    effect_id=effect_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id=self.session_id,
                    run_id=run_id,
                    phase="writer",
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    replay_class=ReplayClass.SAFE,
                    replay_args={"path": "target.txt"},
                ),
            ),
            delivery_intent=DeliveryBatchIntent(
                batch_id=new_batch_id(run_id, 1),
                session_id=self.session_id,
                run_id=run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        seen_requests: list[Any] = []

        def fake_agent_run(req: Any) -> RunResult:
            seen_requests.append(req)
            return RunResult("ok", "done", 2, False, False, False)

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)

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
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
            is_git_repository=lambda _project: True,
        )

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch("codey.operations.task_run.run_hybrid_mode", autospec=True) as mock_hybrid:
            run_task_submission(
                deps,
                TaskSubmission(
                    self.session_id,
                    str(self.project_dir),
                    "research then continue writer",
                    5,
                    False,
                    "mock_provider",
                    intent="hybrid",
                    run_id=run_id,
                ),
            )

        mock_hybrid.assert_not_called()
        self.assertEqual(len(seen_requests), 1)
        request = seen_requests[0]
        self.assertTrue(request.fresh_chat or request.conversation is not None)
        self.assertTrue(request.recovered_tool_outcomes)
        self.assertIn("hybrid recovered file text", request.recovered_tool_outcomes[0].outcome.model_text)
        start_events = [e for e in emitted_events if e.get("type") == "task_start"]
        self.assertEqual(len(start_events), 1)
        self.assertEqual(start_events[0].get("mode"), "agent")
        self.assertTrue(start_events[0].get("continue_task"))

    def test_recovery_failure_finishes_accepted_operation_terminal(self) -> None:
        from codey.runs.details import load_run_details

        state = server.AppContext(state_home=self.temp_dir.name)
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
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
            is_git_repository=lambda _project: True,
        )

        emitted_events: list[dict] = []
        state.emit = lambda event: emitted_events.append(event)
        run_id = "run-pregate-store-isolation-1"

        with patch(
            "codey.operations.task_run.recover_effects_for_resume",
            return_value=ResumeRecoveryResult(ok=False),
        ):
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
                    run_id=run_id,
                ),
            )

        operation = state.runtime_operations.load(self.session_id, run_id)
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation.leaf, "terminal")
        assert operation.terminal is not None
        self.assertEqual(operation.terminal.stop_reason, "error")
        # Registry must NOT be busy
        self.assertFalse(state.run_registry.is_busy())
        # Terminal event is emitted with stop_reason="error"
        done_events = [e for e in emitted_events if e.get("type") == "task_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0].get("stop_reason"), "error")
        # Run details resolves cleanly via trace and error event
        details = load_run_details(
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            session_id=self.session_id,
            run_id=run_id,
            runtime_operations=state.runtime_operations,
            runtime_effects=state.runtime_effects,
        )
        self.assertTrue(details.available)

    def test_tool_call_intent_persists_canonical_replay_args_for_safe_tools(self) -> None:
        session = self._create_session(MockProvider())

        # 1. Safe tool intent (read)
        call_read = ToolCall(name="read", args={"path": "foo.py", "offset": 5})
        _, replay_read = evaluate_tool_call_policy(session, call_read, turn=1, tool_index=0)
        call_edit = ToolCall(name="edit", args={"path": "foo.py", "content": "hello"})
        _, replay_edit = evaluate_tool_call_policy(session, call_edit, turn=1, tool_index=1)
        intent_read = build_tool_call_intent(
            session,
            call_read,
            turn=1,
            tool_index=0,
            replay_decision=replay_read,
        )
        intent_edit = build_tool_call_intent(
            session,
            call_edit,
            turn=1,
            tool_index=1,
            replay_decision=replay_edit,
        )
        assert intent_read is not None
        assert intent_edit is not None
        items = (
            DeliveryBatchItem(0, "read", intent_read.effect_id, "safe", False),
            DeliveryBatchItem(1, "edit", intent_edit.effect_id, "unsafe", False),
        )
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(intent_read, intent_edit),
            delivery_intent=DeliveryBatchIntent(
                batch_id=new_batch_id(self.run_id, 1),
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        loaded = self.effects.load_effects(self.session_id, self.run_id)
        proj_read = next(p for p in loaded if p.intent.effect_id == intent_read.effect_id)
        proj_edit = next(p for p in loaded if p.intent.effect_id == intent_edit.effect_id)
        self.assertEqual(proj_read.intent.replay_args, {"path": "foo.py", "offset": 5})
        self.assertIsNone(proj_edit.intent.replay_args)

    def test_resume_recovers_pending_safe_tool_and_settles_with_replay_count(self) -> None:
        # Write a dummy file to project_dir
        test_file = self.project_dir / "target.txt"
        test_file.write_text("file content to read", encoding="utf-8")

        # Record a pending read intent (simulating crash before settlement)
        session = self._create_session(MockProvider())
        call_read = ToolCall(name="read", args={"path": "target.txt"})
        _, replay_read = evaluate_tool_call_policy(session, call_read, turn=1, tool_index=0)
        intent = build_tool_call_intent(
            session,
            call_read,
            turn=1,
            tool_index=0,
            replay_decision=replay_read,
        )
        assert intent is not None
        eff_read = intent.effect_id
        self._commit_tool_batch(
            intent,
            turn=1,
            tool_index=0,
            tool_name="read",
            replay_class="safe",
        )

        # Resume recovery
        recovery = recover_effects_for_resume(
            self._deps(),
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        recovered = recovery.recovered_tool_outcomes
        self.assertEqual(len(recovered), 1)
        rec = recovered[0]
        self.assertEqual(rec.call.name, "read")
        self.assertTrue(rec.outcome.ok)
        self.assertIn("file content to read", rec.outcome.model_text)

        # Check effect settlement on disk
        loaded = self.effects.load_effects(self.session_id, self.run_id)
        proj = next(p for p in loaded if p.intent.effect_id == eff_read)
        self.assertFalse(proj.is_pending)
        assert proj.settlement is not None
        self.assertEqual(proj.settlement.status, SETTLEMENT_STATUS_OK)
        self.assertEqual(proj.settlement.replay_count, 1)
        self.assertEqual(proj.settlement.replayed_from_effect_id, eff_read)

    def test_resume_replay_uses_writer_profile_for_task_kind(self) -> None:
        from codey.runtime.replay_policy import tool_replay_policy

        test_file = self.project_dir / "target.txt"
        test_file.write_text("file content to read", encoding="utf-8")

        session = self._create_session(MockProvider())
        call_read = ToolCall(name="read", args={"path": "target.txt"})
        _, replay_read = evaluate_tool_call_policy(session, call_read, turn=1, tool_index=0)
        intent = build_tool_call_intent(
            session,
            call_read,
            turn=1,
            tool_index=0,
            replay_decision=replay_read,
        )
        assert intent is not None
        self._commit_tool_batch(
            intent,
            turn=1,
            tool_index=0,
            tool_name="read",
            replay_class="safe",
        )
        seen_profiles: list[str] = []

        def fake_profile(task_kind: str, *, phase: str = "") -> SimpleNamespace:
            self.assertEqual(task_kind, "hybrid")
            self.assertEqual(phase, "writer")
            return SimpleNamespace(name="custom_writer")

        def fake_evaluate(call: ToolCall, **kwargs: Any) -> tuple[None, Any]:
            seen_profiles.append(str(kwargs.get("permission_profile") or ""))
            return None, tool_replay_policy(call.name)

        with (
            patch("codey.operations.recovery.profile_for_task_kind", side_effect=fake_profile),
            patch("codey.operations.recovery.evaluate_tool_call_policy_for", side_effect=fake_evaluate),
        ):
            recovery = recover_effects_for_resume(
                self._deps(),
                session_id=self.session_id,
                run_id=self.run_id,
                project=str(self.project_dir),
                task_kind="hybrid",
            )

        self.assertTrue(recovery.ok)
        self.assertEqual(seen_profiles, ["custom_writer"])
        self.assertEqual(len(recovery.recovered_tool_outcomes), 1)

    def test_resume_does_not_replay_planning_readonly_effects(self) -> None:
        test_file = self.project_dir / "target.txt"
        test_file.write_text("file content to read", encoding="utf-8")

        run_id = "run-planning-readonly"
        self.line.accept_operation(
            session_id=self.session_id,
            run_id=run_id,
            project=str(self.project_dir),
            provider_id="mock_provider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="planning_readonly",
        )
        self.line.mark_writer_running(
            self.session_id,
            run_id,
            provider_id="mock_provider",
        )
        eff_read = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, run_id)
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_read,
                replay_class="safe",
                is_denied=False,
            ),
        )
        self.line.begin_tool_batch(
            self.session_id,
            run_id,
            intents=(
                RuntimeEffectIntent(
                    effect_id=eff_read,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id=self.session_id,
                    run_id=run_id,
                    phase="writer",
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    replay_class=ReplayClass.SAFE,
                    replay_args={"path": "target.txt"},
                ),
            ),
            delivery_intent=DeliveryBatchIntent(
                batch_id=new_batch_id(run_id, 1),
                session_id=self.session_id,
                run_id=run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        recovery = recover_effects_for_resume(
            self._deps(),
            session_id=self.session_id,
            run_id=run_id,
            project=str(self.project_dir),
            task_kind="planning_readonly",
        )

        self.assertTrue(recovery.ok)
        self.assertEqual(recovery.recovered_tool_outcomes, ())
        loaded = self.effects.load_effects(self.session_id, run_id)
        proj = next(p for p in loaded if p.intent.effect_id == eff_read)
        self.assertFalse(proj.is_pending)
        assert proj.settlement is not None
        self.assertEqual(proj.settlement.status, "interrupted")
        self.assertEqual(proj.settlement.replay_count, 0)

    def test_resume_invalid_persisted_replay_args_fails_closed_without_recovery_failure(self) -> None:
        eff_id = "eff_bad_replay_args"
        lane = lane_for_run(self.run_id)
        op_id = operation_id_for_run(self.run_id)
        batch_id = new_batch_id(self.run_id, 1)
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=eff_id,
                replay_class="safe",
                is_denied=False,
            ),
        )
        _commit_log_entry(self.log,
            self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "runtime_effect",
                "record_kind": "intent",
                "ref": f"effect:{eff_id}",
                "effect_id": eff_id,
                "effect_category": "tool_call",
                "session_id": self.session_id,
                "run_id": self.run_id,
                "lane": lane,
                "operation_id": op_id,
                "turn": 1,
                "tool_index": 0,
                "tool_name": "read",
                "replay_class": "safe",
                "replay_args": {"path": "../outside.py"},
            },
        )
        _commit_log_entry(self.log,
            self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload=DeliveryBatchIntent(
                batch_id=batch_id,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ).to_payload(),
        )
        current = self.operations.load(self.session_id, self.run_id)
        assert current is not None
        pending = mark_tool_effect_pending(
            current,
            effect_ids=(eff_id,),
            driver="writer",
            delivery_batch_id=batch_id,
            turn=1,
        )
        _commit_log_entry(
            self.log,
            self.session_id,
            lane=pending.lane,
            operation_id=pending.operation_id,
            kind="operation_state",
            payload=pending.to_payload(),
        )

        recovery = recover_effects_for_resume(
            self._deps(),
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )

        self.assertTrue(recovery.ok)
        self.assertEqual(recovery.recovered_tool_outcomes, ())
        loaded = self.effects.load_effects(self.session_id, self.run_id)
        proj = next(p for p in loaded if p.intent.effect_id == eff_id)
        self.assertIsNone(proj.intent.replay_args)
        self.assertFalse(proj.is_pending)
        assert proj.settlement is not None
        self.assertEqual(proj.settlement.status, "interrupted")
        self.assertEqual(proj.settlement.replay_count, 0)

    def test_resume_replay_propagates_cancellation(self) -> None:
        test_file = self.project_dir / "target.txt"
        test_file.write_text("file content to read", encoding="utf-8")

        session = self._create_session(MockProvider())
        call_read = ToolCall(name="read", args={"path": "target.txt"})
        _, replay_read = evaluate_tool_call_policy(session, call_read, turn=1, tool_index=0)
        intent = build_tool_call_intent(
            session,
            call_read,
            turn=1,
            tool_index=0,
            replay_decision=replay_read,
        )
        assert intent is not None
        eff_read = intent.effect_id
        self._commit_tool_batch(
            intent,
            turn=1,
            tool_index=0,
            tool_name="read",
            replay_class="safe",
        )
        stop = threading.Event()
        stop.set()

        with cancellation.scope(stop), self.assertRaises(cancellation.TaskCancelled):
            recover_effects_for_resume(
                self._deps(),
                session_id=self.session_id,
                run_id=self.run_id,
                project=str(self.project_dir),
                task_kind="project",
            )

        pending = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].intent.effect_id, eff_read)

    def test_resume_synthesizes_interrupted_for_pending_unsafe_tool(self) -> None:
        session = self._create_session(MockProvider())
        call_edit = ToolCall(name="edit", args={"path": "foo.py", "content": "bar"})
        _, replay_edit = evaluate_tool_call_policy(session, call_edit, turn=1, tool_index=0)
        intent = build_tool_call_intent(
            session,
            call_edit,
            turn=1,
            tool_index=0,
            replay_decision=replay_edit,
        )
        assert intent is not None
        eff_edit = intent.effect_id
        self._commit_tool_batch(
            intent,
            turn=1,
            tool_index=0,
            tool_name="edit",
            replay_class="unsafe",
        )

        recovery = recover_effects_for_resume(
            self._deps(),
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertTrue(recovery.ok)
        recovered = recovery.recovered_tool_outcomes
        self.assertEqual(len(recovered), 0)  # Unsafe is NEVER replayed

        loaded = self.effects.load_effects(self.session_id, self.run_id)
        proj = next(p for p in loaded if p.intent.effect_id == eff_edit)
        self.assertFalse(proj.is_pending)
        assert proj.settlement is not None
        self.assertEqual(proj.settlement.status, "interrupted")
        self.assertEqual(proj.settlement.replay_count, 0)

    def test_agent_loop_with_recovered_tool_outcomes_resumes_cleanly(self) -> None:
        from codey.agents.loop import run
        from codey.agents.request import RecoveredToolOutcome

        provider = MockProvider(
            reply='{"tool": "done", "args": {"summary": "task finished after resume"}}'
        )
        recovered_outcome = RecoveredToolOutcome(
            call=ToolCall(name="read", args={"path": "foo.py"}),
            outcome=ToolOutcome("dummy file content", True),
            turn=1,
            tool_index=0,
        )

        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="finish the task",
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            recovered_tool_outcomes=(recovered_outcome,),
        )

        result = run(req)
        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.turns, 2)  # Started from turn 2
        # Provider should have received the formatted tool result instead of initial prompt
        self.assertEqual(len(provider.send_history), 1)
        self.assertIn("dummy file content", provider.send_history[0])

    def test_agent_loop_with_recovered_tool_outcomes_respects_turn_budget(self) -> None:
        from codey.agents.loop import run
        from codey.agents.request import RecoveredToolOutcome

        provider = MockProvider(
            reply='{"tool": "done", "args": {"summary": "should not be sent"}}'
        )
        recovered_outcome = RecoveredToolOutcome(
            call=ToolCall(name="read", args={"path": "foo.py"}),
            outcome=ToolOutcome("dummy file content", True),
            turn=1,
            tool_index=0,
        )

        req = AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="finish the task",
            max_turns=1,
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_effects=self.effects,
            recovered_tool_outcomes=(recovered_outcome,),
        )

        result = run(req)
        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(result.turns, 1)
        self.assertEqual(provider.send_history, [])


if __name__ == "__main__":
    unittest.main()
