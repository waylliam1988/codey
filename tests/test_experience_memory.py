"""Acceptance tests for the experience-memory final state.

Covers the four agreed acceptance items:

1. Unified auto runs in the same batch (first normal call decides
   answer-vs-action; project writer lock is deferred until an edit action).
2. Settlement owns final display: observation persisted before reply/task_done;
   write failure is reported as Ghost-record-failed, never as recoverable.
3. Observations live under the Ghost control plane (disable/view/export/
   delete/retention); no per-signal reinforcement concepts remain.
4. Call counts vs baseline (ordinary auto chat == 1 normal inference; Ghost
   paths == 0 model calls) plus strict retrieval token budgets.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.agents.handoff import ConversationContext
from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    retrieve_relevant_observations,
)
from codey.ghost.observations import GhostObservationStore
from codey.operations import ghost_post_turn
from codey.operations.auto_loop import (
    AutoRunDeps,
    check_auto_action_permitted,
    parse_auto_first_output,
    run_auto_mode,
    strip_action_markers,
)
from codey.operations.result import ModeOutcome
from codey.operations.task_phases import settlement
from codey.task.model import TaskSubmission


class _FakeProvider:
    name = "Fake"

    def __init__(self, reply: str = "hello") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.send_calls = 0

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.send_calls += 1
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        pass


def _auto_frame(*, task: str, project: str = "", provider: _FakeProvider | None = None):
    provider = provider if provider is not None else _FakeProvider()
    return SimpleNamespace(
        request=TaskSubmission("session-1", project or None, task, 8, False, "local"),
        run_id="run-1",
        task_kind="project" if project else "chat",
        provider=provider,
        provider_id="local",
        project_text=str(Path(project).expanduser().resolve()) if project else "",
        conversation=ConversationContext(),
        fresh_chat=True,
        handoff="",
        trace=None,
        recovered_tool_outcomes=(),
    )


def _auto_deps(state, mode_deps, **overrides):
    params = {
        "state": state,
        "mode_deps": mode_deps,
        "config_result": None,
        "acquire_writer": lambda _project: True,
        "release_writer": lambda _project: None,
        "open_ledger_for": lambda _kind: None,
        "ghost_directive_fn": None,
        "ghost_continuity_fn": None,
        "experiences_fn": None,
        "has_reviewable_diff_fn": None,
        "research_available": True,
    }
    params.update(overrides)
    return AutoRunDeps(**params)


class AutoFirstOutputTests(unittest.TestCase):
    def test_plain_text_is_a_direct_answer(self) -> None:
        decision = parse_auto_first_output("你好，我是助手。")
        self.assertEqual(decision.kind, "chat")
        self.assertEqual(decision.answer, "你好，我是助手。")

    def test_action_marker_selects_mode_and_keeps_plan(self) -> None:
        decision = parse_auto_first_output("ACTION: research\nPLAN: 查今天的发布记录")
        self.assertEqual(decision.kind, "research")
        self.assertIn("查今天的发布记录", decision.plan)

    def test_planning_alias_normalizes(self) -> None:
        decision = parse_auto_first_output("ACTION: planning\nPLAN: x")
        self.assertEqual(decision.kind, "planning_readonly")

    def test_unknown_action_is_an_answer_not_a_leak(self) -> None:
        raw = "ACTION: teleport\nPLAN: go elsewhere"
        decision = parse_auto_first_output(raw)
        self.assertEqual(decision.kind, "chat")
        self.assertEqual(decision.answer, raw)

    def test_project_action_needs_a_project(self) -> None:
        decision = parse_auto_first_output("ACTION: project\nPLAN: fix it")
        permitted, _ = check_auto_action_permitted(decision, project="")
        self.assertFalse(permitted)

    def test_review_action_needs_a_diff(self) -> None:
        decision = parse_auto_first_output("ACTION: review\nPLAN: look")
        permitted, _ = check_auto_action_permitted(
            decision, project="/repo", has_reviewable_diff=lambda: False,
        )
        self.assertFalse(permitted)

    def test_denied_action_strips_markers_for_display(self) -> None:
        shown = strip_action_markers("ACTION: project\nPLAN: fix it")
        self.assertNotIn("ACTION", shown)


class UnifiedAutoCallCountTests(unittest.TestCase):
    def test_ordinary_auto_chat_is_exactly_one_normal_inference(self) -> None:
        provider = _FakeProvider("你好！有什么可以帮你？")
        frame = _auto_frame(task="你是谁", provider=provider)
        state = mock.Mock()
        state.set_provider_session.return_value = True
        outcome = run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _auto_deps(state, SimpleNamespace(), open_ledger_for=mock.Mock()),
        )
        self.assertEqual(provider.send_calls, 1)
        self.assertEqual(outcome.event["mode"], "chat")
        self.assertEqual(outcome.display[0]["type"], "reply")
        self.assertEqual(outcome.display[0]["text"], "你好！有什么可以帮你？")

    def test_action_plan_is_forwarded_not_discarded(self) -> None:
        provider = _FakeProvider("ACTION: research\nPLAN: 查今天的发布记录")
        frame = _auto_frame(task="查一下今天的消息", provider=provider)
        seen: dict[str, str] = {}

        def run_research(active_frame, _hooks):
            seen["task"] = active_frame.request.task
            return ModeOutcome({"type": "task_done", "mode": "research",
                                "summary": "researched", "stop_reason": "done",
                                "run_id": "run-1", "session_id": "session-1",
                                "turns": 1, "max_turns": 8, "provider": "local"})

        mode_deps = SimpleNamespace(
            research=run_research,
            project=mock.Mock(), planning=mock.Mock(), review=mock.Mock(),
            chat=mock.Mock(),
        )
        opened: list[str] = []
        outcome = run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _auto_deps(mock.Mock(), mode_deps,
                       open_ledger_for=lambda kind: opened.append(kind)),
        )
        self.assertEqual(provider.send_calls, 1)
        self.assertEqual(outcome.event["mode"], "research")
        self.assertIn("查今天的发布记录", seen["task"])
        self.assertIn("查一下今天的消息", seen["task"])
        self.assertEqual(opened, ["research"])

    def test_first_call_failure_falls_back_to_baseline(self) -> None:
        class _DeadProvider(_FakeProvider):
            def send(self, text: str, timeout: float | None = None) -> str:
                raise RuntimeError("offline")

        frame = _auto_frame(task="修复测试", project="/repo", provider=_DeadProvider())
        ran: list[str] = []

        def run_project(*_args, **_kwargs):
            ran.append("project")
            return ModeOutcome({"type": "task_done", "mode": "project",
                                "summary": "fixed", "stop_reason": "done",
                                "run_id": "run-1", "session_id": "session-1",
                                "turns": 1, "max_turns": 8, "provider": "local"})

        mode_deps = SimpleNamespace(
            project=run_project, research=mock.Mock(),
            planning=mock.Mock(), review=mock.Mock(), chat=mock.Mock(),
        )
        outcome = run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _auto_deps(mock.Mock(), mode_deps),
        )
        self.assertEqual(ran, ["project"])
        self.assertEqual(outcome.event["summary"], "fixed")

    def test_project_action_acquires_writer_lazily(self) -> None:
        provider = _FakeProvider("ACTION: project\nPLAN: 修测试")
        frame = _auto_frame(task="修测试", project="/repo", provider=provider)
        acquired: list[str] = []
        released: list[str] = []

        def run_project(*_args, **_kwargs):
            acquired.append("during-run")
            return ModeOutcome({"type": "task_done", "mode": "project",
                                "summary": "fixed", "stop_reason": "done",
                                "run_id": "run-1", "session_id": "session-1",
                                "turns": 1, "max_turns": 8, "provider": "local"})

        mode_deps = SimpleNamespace(
            project=run_project, research=mock.Mock(),
            planning=mock.Mock(), review=mock.Mock(), chat=mock.Mock(),
        )
        run_auto_mode(
            frame, SimpleNamespace(), SimpleNamespace(),
            _auto_deps(mock.Mock(), mode_deps,
                       acquire_writer=lambda project: acquired.append(project) or True,
                       release_writer=lambda project: released.append(project)),
        )
        # Writer claimed only after the action was chosen, released after run.
        self.assertTrue(acquired)
        self.assertEqual(acquired[0], frame.project_text)
        self.assertEqual(released, [frame.project_text])


class GhostZeroModelCallTests(unittest.TestCase):
    def test_pre_turn_routing_makes_no_model_call(self) -> None:
        factory = mock.Mock(side_effect=AssertionError("must not be called"))
        deps = SimpleNamespace(
            state=SimpleNamespace(ghost_router=None),
            router_provider_factory=factory,
        )
        request = TaskSubmission("s", None, "hello", 8, False, "local")
        self.assertIsNone(ghost_post_turn.maybe_route_auto(
            deps, request, baseline_mode="chat", run_id="r"))
        factory.assert_not_called()

    def test_post_turn_learning_makes_no_model_call(self) -> None:
        factory = mock.Mock(side_effect=AssertionError("must not be called"))
        state = mock.Mock()
        deps = SimpleNamespace(
            state=state,
            learning_provider_factory=factory,
            learning_modes=("chat",),
        )
        frame = SimpleNamespace(
            run_id="r", provider_id="local", project_text="",
            request=SimpleNamespace(task="hi", session_id="s"),
        )
        ghost_post_turn.maybe_run_learning(
            deps, frame, {"mode": "chat", "stop_reason": "done"})
        factory.assert_not_called()
        skipped = next(
            call for call in state.emit.call_args_list
            if call.args[0].get("type") == "ghost_learning_done"
        )
        self.assertEqual(skipped.args[0]["skipped_reason"], "replaced_by_observations")


class ObservationStoreTests(unittest.TestCase):
    def test_replay_same_run_id_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            for _ in range(2):
                self.assertTrue(store.append_completed(
                    run_id="r1", session_id="s", mode="chat",
                    user_text="你是谁", assistant_text="我是助手",
                    stop_reason="done", provider_id="local"))
            self.assertEqual(len(store.read_all()), 1)

    def test_only_done_rounds_are_retrievable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            store.append_completed(
                run_id="ok", session_id="s", mode="chat",
                user_text="hi", assistant_text="hello",
                stop_reason="done", provider_id="local")
            store.append_completed(
                run_id="bad", session_id="s", mode="chat",
                user_text="hi", assistant_text="",
                stop_reason="error", provider_id="local")
            rows = store.read_committed(session_id="s")
            self.assertEqual([row["run_id"] for row in rows], ["ok"])

    def test_delete_scope_removes_residual_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            store.append_completed(
                run_id="r1", session_id="s", mode="chat",
                user_text="以后回答短一点", assistant_text="好的",
                stop_reason="done", provider_id="local")
            self.assertGreater(store.delete_scope("session", session_id="s"), 0)
            self.assertEqual(store.read_committed(session_id="s"), ())

    def test_cjk_paraphrase_retrieves_without_shared_word_runs(self) -> None:
        """Regression: Chinese has no spaces, so punctuation-split word runs
        almost never overlap. CJK bigram matching must find related rounds."""
        rows = [{
            "run_id": "r1",
            "user_text": "请记住：以后回答尽量简短，不超过两句话",
            "assistant_text": "好的",
            "mode": "chat",
            "ts": "2026-09-26T00:00:00Z",
        }]
        picked = retrieve_relevant_observations(rows, "介绍北京，要求简短一点")
        self.assertEqual([row["run_id"] for row in picked], ["r1"])

    def test_retrieval_budget_and_isolation(self) -> None:
        rows = [
            {"run_id": f"r{i}", "user_text": f"第{i}条 以后回答短一点 短一点",
             "assistant_text": "好的", "mode": "chat", "ts": "2026-09-26T00:00:00Z"}
            for i in range(10)
        ]
        picked = retrieve_relevant_observations(rows, "以后回答短一点")
        self.assertLessEqual(len(picked), MAX_RETRIEVED_ITEMS)
        self.assertLessEqual(
            sum(len(f"{r['user_text']}\n{r['assistant_text']}") for r in picked),
            RETRIEVAL_BUDGET_CHARS)
        foreign = retrieve_relevant_observations(
            [{"run_id": "x", "user_text": "aaaaaaaa bb",
              "assistant_text": "", "mode": "chat", "ts": ""}],
            "以后回答短一点")
        self.assertEqual(foreign, ())


class SettlementCommitOrderTests(unittest.TestCase):
    def _settlement_deps(self, state_home: Path, emitted: list[dict]):
        store = GhostObservationStore(state_home)

        class _State:
            ghost_inbox = None

            def __init__(self) -> None:
                self.ghost_observations = store

            def emit(self, payload: dict) -> None:
                emitted.append(payload)

            def finish_run(self, run_id: str, event: dict) -> bool:
                emitted.append({"type": "task_done", **event})
                return True

        return SimpleNamespace(state=_State(), run_ledgers=None)

    def test_display_emitted_only_after_observation_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            emitted: list[dict] = []
            deps = self._settlement_deps(Path(td), emitted)
            frame = SimpleNamespace(
                run_id="r1",
                task_kind="chat",
                request=TaskSubmission("s", None, "你是谁", 8, False, "local"),
            )
            work = SimpleNamespace(claimed_work_item=None, operation=None, ledger=None)
            outcome = ModeOutcome(
                {"type": "task_done", "run_id": "r1", "session_id": "s",
                 "summary": "我是助手", "stop_reason": "done", "turns": 1,
                 "max_turns": 8, "provider": "local", "mode": "chat"},
                display=({"type": "reply", "run_id": "r1",
                          "session_id": "s", "text": "我是助手"},),
            )
            ghost_deps = SimpleNamespace(state=deps.state, run_ledgers=None)
            settlement._finish_mode_outcome(
                deps, ghost_deps, frame, work, outcome,
                append_ledger=lambda _action: None,
                finish_trace=lambda _event: None,
            )
            kinds = [event.get("type") for event in emitted]
            self.assertIn("reply", kinds)
            self.assertIn("task_done", kinds)
            self.assertLess(kinds.index("reply"), kinds.index("task_done"))
            rows = deps.state.ghost_observations.read_committed(session_id="s")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], "r1")

    def test_observation_records_visible_reply_not_internal_summary(self) -> None:
        """assistant_text must be what the user actually saw (display reply
        when the mode publishes one), never the internal settlement summary."""
        with tempfile.TemporaryDirectory() as td:
            emitted: list[dict] = []
            deps = self._settlement_deps(Path(td), emitted)
            frame = SimpleNamespace(
                run_id="r1",
                task_kind="project",
                request=TaskSubmission("s", "/repo", "fix it", 8, False, "local"),
            )
            work = SimpleNamespace(claimed_work_item=None, operation=None, ledger=None)
            outcome = ModeOutcome(
                {"type": "task_done", "run_id": "r1", "session_id": "s",
                 "summary": "completed project task", "stop_reason": "done",
                 "turns": 2, "max_turns": 8, "provider": "local", "mode": "project"},
                display=({"type": "reply", "run_id": "r1",
                          "session_id": "s",
                          "text": "已经修好了，问题在 xxx，我修改了 xxx。"},),
            )
            settlement._finish_mode_outcome(
                deps, SimpleNamespace(state=deps.state, run_ledgers=None),
                frame, work, outcome,
                append_ledger=lambda _action: None,
                finish_trace=lambda _event: None,
            )
            rows = deps.state.ghost_observations.read_committed(session_id="s")
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0]["assistant_text"], "已经修好了，问题在 xxx，我修改了 xxx。")

    def test_observation_falls_back_to_summary_without_display(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            emitted: list[dict] = []
            deps = self._settlement_deps(Path(td), emitted)
            frame = SimpleNamespace(
                run_id="r1",
                task_kind="research",
                request=TaskSubmission("s", None, "research x", 8, False, "local"),
            )
            work = SimpleNamespace(claimed_work_item=None, operation=None, ledger=None)
            outcome = ModeOutcome(
                {"type": "task_done", "run_id": "r1", "session_id": "s",
                 "summary": "synthesis text", "stop_reason": "done", "turns": 1,
                 "max_turns": 8, "provider": "local", "mode": "research"},
            )
            settlement._finish_mode_outcome(
                deps, SimpleNamespace(state=deps.state, run_ledgers=None),
                frame, work, outcome,
                append_ledger=lambda _action: None,
                finish_trace=lambda _event: None,
            )
            rows = deps.state.ghost_observations.read_committed(session_id="s")
            self.assertEqual(rows[0]["assistant_text"], "synthesis text")

    def test_observation_failure_still_answers_but_warns_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            emitted: list[dict] = []
            deps = self._settlement_deps(Path(td), emitted)
            with mock.patch.object(
                deps.state.ghost_observations, "append_completed",
                return_value=False,
            ):
                frame = SimpleNamespace(
                    run_id="r1",
                    task_kind="chat",
                    request=TaskSubmission("s", None, "hi", 8, False, "local"),
                )
                work = SimpleNamespace(claimed_work_item=None, operation=None, ledger=None)
                outcome = ModeOutcome(
                    {"type": "task_done", "run_id": "r1", "session_id": "s",
                     "summary": "hello", "stop_reason": "done", "turns": 1,
                     "max_turns": 8, "provider": "local", "mode": "chat"},
                    display=({"type": "reply", "run_id": "r1",
                              "session_id": "s", "text": "hello"},),
                )
                settlement._finish_mode_outcome(
                    deps, SimpleNamespace(state=deps.state, run_ledgers=None),
                    frame, work, outcome,
                    append_ledger=lambda _action: None,
                    finish_trace=lambda _event: None,
                )
            kinds = [event.get("type") for event in emitted]
            self.assertIn("reply", kinds)
            warning = next(
                event for event in emitted
                if event.get("type") == "ghost_post_turn_warning"
            )
            self.assertEqual(warning["error_ref"], "ghost_observation_failed")


if __name__ == "__main__":
    unittest.main()
