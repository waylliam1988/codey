"""Deterministic tests for agent effect sandwich (intent -> real effect -> settlement)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from codey.agents.request import AgentRequest
from codey.agents.state import AgentLoopSession, RunResult
from codey.agents.tool_execution import (
    evaluate_tool_call_policy_for,
    policy_denied,
)
from codey.agents.tools import AgentToolFns
from codey.app import server
from codey.app import task_submit as task_submit
from codey.operations.recovery import ResumeRecoveryResult, recover_effects_for_resume
from codey.operations.task_entry import run_task_submission
from codey.operations.task_run import TaskRunDeps
from codey.operations.task_run import start_run_operation as _start_run_operation
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall
from codey.runtime.core.operation_state import (
    RuntimeOperationStore,
    lane_for_run,
    mark_tool_effect_pending,
    operation_id_for_run,
)
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectIntent,
    RuntimeEffectStore,
    new_effect_id,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
    new_batch_id,
)
from codey.runtime.log.entries import RuntimeLogEntry
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.task.model import TaskSubmission
from codey.toolchain.runtime import ToolOutcome
from tests.support.kernel_harness import build_kernel_fixture


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

    def new_chat(self) -> None:
        self.send_history.clear()

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
        return build_kernel_fixture(AgentRequest(
            provider=provider,
            project=self.project_dir,
            task="do something",
            provider_id="mock_provider",
            max_turns=10,
            fresh_chat=False,
            coding_context_enabled=False,
            on_event=lambda _event: None,
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_mutations=self.line,
            tool_fns=AgentToolFns(
                read_file=lambda *a, **kw: ToolOutcome("file text", True),
                edit_file=lambda *a, **kw: ToolOutcome("edited", True, changed=True),
                write_file=lambda *a, **kw: ToolOutcome("written", True, changed=True),
                list_directory=lambda *a, **kw: ToolOutcome("dir listing", True),
                search_files=lambda *a, **kw: ToolOutcome("search results", True),
                find_references=lambda *a, **kw: ToolOutcome("refs", True),
                run_command=lambda *a, **kw: ToolOutcome("command output", True),
            ),
        ))

    def _begin_sink_turn(self, call: ToolCall, *, turn: int, tool_index: int) -> str:
        """生产入口：经 KernelEffectSink 提交本轮意图（含交付 envelope）。"""
        from codey.operations.task_effects import KernelEffectSink
        from codey.operations.task_session import turn_effect_id

        effect_id = turn_effect_id(self.run_id, turn, tool_index)
        sink = KernelEffectSink(
            self.line, session_id=self.session_id, run_id=self.run_id,
            provider_id="mock_provider",
        )
        sink.begin_turn([(effect_id, call, tool_index)], turn=turn)
        return effect_id

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
        call = ToolCall("bash", {"command": "echo unsafe", "path": "."})

        policy_decision, replay_decision = evaluate_tool_call_policy_for(
            call,
            project=self.project_dir,
            permission_profile="coding_writer",
            approval_available=False,
            phase="writer",
        )

        self.assertIsNotNone(policy_decision)
        assert policy_decision is not None
        self.assertTrue(policy_denied(policy_decision))
        self.assertEqual(policy_decision.reason_code, "unknown_action")
        self.assertEqual(policy_decision.kind, "unknown_tool")
        self.assertEqual(getattr(replay_decision, "reason", ""), "policy_denied")

    def test_provider_send_intent_and_settlement_on_success(self) -> None:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        provider = MockProvider("hello model")
        sink = KernelEffectSink(
            self.line, session_id=self.session_id, run_id=self.run_id,
            provider_id="mock_provider",
        )
        recorded = KernelRecordedProvider(provider, sink)
        reply = recorded.send("user prompt")
        self.assertEqual(reply, "hello model")

        effects = self.effects.load_effects(self.session_id, self.run_id)
        self.assertEqual(len(effects), 1)
        proj = effects[0]
        self.assertEqual(proj.intent.effect_category, "provider_send")
        self.assertTrue(proj.is_settled)
        self.assertEqual(proj.settlement.status, SETTLEMENT_STATUS_OK)
        self.assertEqual(proj.settlement.sent_state, "settled")

    def test_provider_send_intent_and_settlement_on_error(self) -> None:
        from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

        provider = MockProvider(fail=True)
        sink = KernelEffectSink(
            self.line, session_id=self.session_id, run_id=self.run_id,
            provider_id="mock_provider",
        )
        recorded = KernelRecordedProvider(provider, sink)
        with self.assertRaises(RuntimeError):
            recorded.send("user prompt")

        effects = self.effects.load_effects(self.session_id, self.run_id)
        self.assertEqual(len(effects), 1)
        proj = effects[0]
        self.assertTrue(proj.is_settled)
        self.assertEqual(proj.settlement.status, SETTLEMENT_STATUS_ERROR)
        self.assertEqual(proj.settlement.sent_state, "maybe_sent")

    def test_tool_call_effect_sandwich_sequence(self) -> None:
        # 当前三明治：KernelEffectSink 提交意图 → 真实执行 → 结算 → 交付恢复。
        from codey.agents.tool_execution import execute_information_tool_call
        from codey.operations.task_effects import KernelEffectSink
        from codey.runtime.core.models import ToolResult

        session = self._create_session(MockProvider())
        call = ToolCall(name="read", args={"path": "foo.py"})
        effect_id = self._begin_sink_turn(call, turn=1, tool_index=0)
        self.assertTrue(bool(effect_id))

        # Pending should now have 1 effect
        pending = self.effects.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].intent.tool_name, "read")

        # Execute the real tool (production information-tool path)
        outcome = execute_information_tool_call(
            self.project_dir, session.request.tool_fns, call,
        )
        self.assertTrue(outcome.ok)

        # Settle through the production sink
        sink = KernelEffectSink(
            self.line, session_id=self.session_id, run_id=self.run_id,
            provider_id="mock_provider",
        )
        sink.settle(
            effect_id, True,
            result=ToolResult(call=call, model_text=outcome.model_text),
        )

        # Verify no pending effects
        self.assertEqual(len(self.effects.pending_effects(self.session_id, self.run_id)), 0)
        settled_effects = self.effects.load_effects(self.session_id, self.run_id)
        self.assertEqual(settled_effects[0].settlement.status, SETTLEMENT_STATUS_OK)

    def test_resume_recovery_failure_fails_closed(self) -> None:
        # If reducer-selected recovery needs the effect ledger and it cannot load,
        # recovery fails closed.
        call = ToolCall(name="read", args={"path": "foo.py"})
        self._begin_sink_turn(call, turn=1, tool_index=0)
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
        # Old begin_tool_batch API deleted with the old loop; batch failure
        # without execution is now locked via the new entry batch-mismatch
        # test (test_convergence_repro_locks batch-abort, no receipt overwrite).
        # This placeholder keeps the behavior category (no execution on batch
        # failure) via the new entry.
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall

        policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
        session = TaskSession(policy=policy, task_kind="project", project="demo", max_turns=2)
        executed: list[str] = []

        def _exec(call: ToolCall) -> object:
            executed.append(str(getattr(call, "name", "")))
            from codey.runtime.core.models import ToolResult

            return ToolResult(call=call, model_text="ok")

        # Mismatched recovery batch must abort without executing (new entry).
        from codey.operations.task_session import turn_effect_id
        from codey.runtime.effects.effect_records import compute_args_digest

        good_identity = turn_effect_id("r-batch-fail", 1, 0)
        session.executed[good_identity] = {
            "name": "edit", "ok": True, "call_id": "c0", "excerpt": "ok",
            "args_digest": compute_args_digest({"path": "a.txt", "content": "hello"}),
        }
        bad = ToolCall(name="edit", args={"path": "a.txt", "content": "OTHER"}, call_id="c0b")
        results = execute_turn(session, [bad], executors={"edit": _exec}, run_id="r-batch-fail", turn=1)
        self.assertTrue(str(results[0].model_text).startswith("ERROR:"))
        self.assertEqual(executed, [])

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
            capture_provider_failure=task_submit.capture_provider_failure,
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
            capture_provider_failure=task_submit.capture_provider_failure,
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
            capture_provider_failure=task_submit.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            workspace_revisions=state.workspace_revisions,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            runtime_mutations=state.runtime_mutations,
            runtime_effects=self.effects,
            is_git_repository=lambda _project: True,
        )

        with patch.object(state, "get_provider", return_value=MockProvider()), \
             patch("codey.operations.task_phases.ghost.maybe_claim_work_item") as claim_work_item, \
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

    def test_recovery_fails_before_provider_send(self) -> None:
        state = server.AppContext(state_home=self.temp_dir.name)

        deps = TaskRunDeps(
            state=state,
            agent_run=Mock(),
            collect_changes=Mock(return_value={"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"}),
            run_review=Mock(return_value=None),
            capture_provider_failure=task_submit.capture_provider_failure,
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
                    run_id="run-auto-recovery-gate-1",
                ),
            )

        # Recovery failed before provider dispatch.
        # Registry must NOT be busy
        self.assertFalse(state.run_registry.is_busy())
        # Terminal event must be stop_reason="error"
        done_events = [e for e in emitted_events if e.get("type") == "task_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0].get("stop_reason"), "error")

    def test_recovered_tool_outcomes_skip_work_claim(self) -> None:
        state = server.AppContext()
        run_id = "run-recovered-skip-claim-1"
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
        from codey.operations.recovery import record_entry_policy
        from codey.policies.task_policy import TaskPolicy

        original_policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
        record_entry_policy(state.runtime_mutations, session_id=self.session_id,
                            run_id=run_id, policy=original_policy)
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
            self.assertEqual(req.task_policy, original_policy)
            return RunResult("ok", "done", 2, False, False, False)

        deps = TaskRunDeps(
            state=state,
            agent_run=fake_agent_run,
            collect_changes=Mock(return_value={"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"}),
            run_review=Mock(return_value=None),
            capture_provider_failure=task_submit.capture_provider_failure,
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
             patch("codey.operations.task_phases.ghost.maybe_claim_work_item", autospec=True) as mock_claim:
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
        self.assertEqual(len(seen_requests), 1)
        recovered = seen_requests[0].recovered_tool_outcomes
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].call.name, "read")
        self.assertIn("recovered file text", recovered[0].outcome.model_text)

    def test_recovered_hybrid_keeps_kind_and_delivers_real_kernel_results(self) -> None:
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

        from codey.operations.recovery import record_entry_policy
        from codey.policies.task_policy import TaskPolicy

        record_entry_policy(state.runtime_mutations, session_id=self.session_id, run_id=run_id,
                            policy=TaskPolicy(grants=frozenset({"control", "project.read"})))
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
            capture_provider_failure=task_submit.capture_provider_failure,
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

        provider = MockProvider(reply='{"tool":"done","args":{"summary":"read complete"}}')
        with patch.object(state, "get_provider", return_value=provider):
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

        self.assertEqual(seen_requests, [])
        self.assertEqual(len(provider.send_history), 1)
        self.assertIn("hybrid recovered file text", provider.send_history[0])
        done_events = [event for event in emitted_events if event.get("type") == "task_done"]
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0]["stop_reason"], "done")
        start_events = [e for e in emitted_events if e.get("type") == "task_start"]
        self.assertEqual(len(start_events), 1)
        self.assertEqual(start_events[0].get("mode"), "hybrid")
        self.assertTrue(start_events[0].get("continue_task"))

    def test_recovery_failure_finishes_accepted_operation_terminal(self) -> None:
        from codey.runs.details import load_run_details

        state = server.AppContext(state_home=self.temp_dir.name)
        deps = TaskRunDeps(
            state=state,
            agent_run=Mock(),
            collect_changes=Mock(return_value={"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"}),
            run_review=Mock(return_value=None),
            capture_provider_failure=task_submit.capture_provider_failure,
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
        # 1. Safe tool intent (read) + unsafe tool intent (edit) via the sink
        call_read = ToolCall(name="read", args={"path": "foo.py", "offset": 5})
        call_edit = ToolCall(name="edit", args={"path": "foo.py", "content": "hello"})
        from codey.operations.task_effects import KernelEffectSink

        sink = KernelEffectSink(
            self.line, session_id=self.session_id, run_id=self.run_id,
            provider_id="mock_provider",
        )
        eff_read = "eff-sink-read"
        eff_edit = "eff-sink-edit"
        sink.begin_turn([(eff_read, call_read, 0), (eff_edit, call_edit, 1)], turn=1)
        loaded = self.effects.load_effects(self.session_id, self.run_id)
        proj_read = next(p for p in loaded if p.intent.effect_id == eff_read)
        proj_edit = next(p for p in loaded if p.intent.effect_id == eff_edit)
        self.assertEqual(proj_read.intent.replay_args, {"path": "foo.py", "offset": 5})
        self.assertIsNone(proj_edit.intent.replay_args)

    def test_resume_recovers_pending_safe_tool_and_settles_with_replay_count(self) -> None:
        # Write a dummy file to project_dir
        test_file = self.project_dir / "target.txt"
        test_file.write_text("file content to read", encoding="utf-8")

        # Record a pending read intent (simulating crash before settlement)
        call_read = ToolCall(name="read", args={"path": "target.txt"})
        eff_read = self._begin_sink_turn(call_read, turn=1, tool_index=0)

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
        from codey.runtime.effects.replay_policy import tool_replay_policy

        test_file = self.project_dir / "target.txt"
        test_file.write_text("file content to read", encoding="utf-8")

        call_read = ToolCall(name="read", args={"path": "target.txt"})
        self._begin_sink_turn(call_read, turn=1, tool_index=0)
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

        self.assertFalse(recovery.ok)  # Interrupted tool has no trustworthy result receipt.
        self.assertEqual(recovery.recovered_tool_outcomes, ())
        loaded = self.effects.load_effects(self.session_id, run_id)
        proj = next(p for p in loaded if p.intent.effect_id == eff_read)
        self.assertFalse(proj.is_pending)
        assert proj.settlement is not None
        self.assertEqual(proj.settlement.status, "interrupted")
        self.assertEqual(proj.settlement.replay_count, 0)

    def test_resume_invalid_persisted_replay_args_stops_without_executing(self) -> None:
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
            driver="writer",
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

        self.assertFalse(recovery.ok)  # No original observation can be reconstructed.
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

        call_read = ToolCall(name="read", args={"path": "target.txt"})
        eff_read = self._begin_sink_turn(call_read, turn=1, tool_index=0)
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
        call_edit = ToolCall(name="edit", args={"path": "foo.py", "content": "bar"})
        eff_edit = self._begin_sink_turn(call_edit, turn=1, tool_index=0)

        recovery = recover_effects_for_resume(
            self._deps(),
            session_id=self.session_id,
            run_id=self.run_id,
            project=str(self.project_dir),
            task_kind="project",
        )
        self.assertFalse(recovery.ok)  # Interrupted tool has no trustworthy result receipt.
        recovered = recovery.recovered_tool_outcomes
        self.assertEqual(len(recovered), 0)  # Unsafe is NEVER replayed

        loaded = self.effects.load_effects(self.session_id, self.run_id)
        proj = next(p for p in loaded if p.intent.effect_id == eff_edit)
        self.assertFalse(proj.is_pending)
        assert proj.settlement is not None
        self.assertEqual(proj.settlement.status, "interrupted")
        self.assertEqual(proj.settlement.replay_count, 0)

    def test_agent_loop_with_recovered_tool_outcomes_resumes_cleanly(self) -> None:
        from codey.agents.request import RecoveredToolOutcome
        from codey.operations.project_adapter import run as run_kernel_request

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
            provider_id="mock_provider",
            project=self.project_dir,
            task="finish the task",
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_mutations=self.line,
            recovered_tool_outcomes=(recovered_outcome,),
        )

        result = run_kernel_request(req)
        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.turns, 2)  # Started from turn 2
        # Provider should have received the formatted tool result instead of initial prompt
        self.assertEqual(len(provider.send_history), 1)
        self.assertIn("dummy file content", provider.send_history[0])

    def test_agent_loop_with_recovered_tool_outcomes_respects_turn_budget(self) -> None:
        from codey.agents.request import RecoveredToolOutcome
        from codey.operations.project_adapter import run as run_kernel_request

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
            provider_id="mock_provider",
            project=self.project_dir,
            task="finish the task",
            max_turns=1,
            session_id=self.session_id,
            run_id=self.run_id,
            runtime_mutations=self.line,
            recovered_tool_outcomes=(recovered_outcome,),
        )

        result = run_kernel_request(req)
        self.assertEqual(result.stop_reason, "max_turns")
        # New single entry counts only new turns (recovered turn=1 exhausts
        # budget max_turns=1, so 0 new turns, no provider call).
        self.assertEqual(result.turns, 0)
        self.assertEqual(provider.send_history, [])


if __name__ == "__main__":
    unittest.main()
