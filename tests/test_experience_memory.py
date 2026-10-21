"""Experience retrieval, retention and settlement need no model calls."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    render_retrieved_block,
    retrieve_relevant_observations,
)
from codey.ghost.observations import MAX_OBSERVATIONS, GhostObservationStore
from codey.operations.result import ModeOutcome
from codey.operations.task_phases import settlement
from codey.task.model import TaskSubmission


class GhostZeroModelCallTests(unittest.TestCase):
    def test_post_turn_needs_no_provider_factory(self) -> None:
        """Post-turn Ghost work takes no provider factory: there is nothing
        left that could spend a model call here."""
        import inspect

        from codey.operations.ghost_post_turn import (
            GhostTaskPolicyDeps,
            run_ghost_post_turn,
        )
        params = set(inspect.signature(GhostTaskPolicyDeps).parameters)
        self.assertNotIn("router_provider_factory", params)
        self.assertNotIn("learning_provider_factory", params)
        self.assertFalse(hasattr(run_ghost_post_turn, "__wrapped__"))
        deps = GhostTaskPolicyDeps(state=SimpleNamespace())
        self.assertIsNone(run_ghost_post_turn(
            deps, None,
            {"mode": "chat", "stop_reason": "done",
             "run_id": "r", "session_id": "s"},
            None,
        ))


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

    def test_agent_mode_is_preserved_not_remapped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            self.assertTrue(store.append_completed(
                run_id="r1", session_id="s", mode="agent",
                user_text="hi", assistant_text="hello",
                stop_reason="done", provider_id="local"))
            rows = store.read_all()
            self.assertEqual(rows[0]["mode"], "agent")

    def test_delete_scope_removes_residual_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            store.append_completed(
                run_id="r1", session_id="s", mode="chat",
                user_text="以后回答短一点", assistant_text="好的",
                stop_reason="done", provider_id="local")
            self.assertGreater(store.delete_scope("session", session_id="s"), 0)
            self.assertEqual(store.read_committed(session_id="s"), ())

    def test_sqlite_database_is_not_treated_as_a_jsonl_event_log(self) -> None:
        """Ghost facts are stored in the independent SQLite owner."""
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            for run_id in ("r1", "r2"):
                self.assertTrue(store.append_completed(
                    run_id=run_id, session_id="s", mode="chat",
                    user_text="hi", assistant_text="hello",
                    stop_reason="done", provider_id="local"))
            self.assertTrue(store.database_path.is_file())
            self.assertFalse((Path(td) / "ghost" / "observations.jsonl").exists())
            self.assertEqual([row["run_id"] for row in store.read_all()], ["r1", "r2"])

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
        rendered = render_retrieved_block(picked, RETRIEVAL_BUDGET_CHARS)
        item_chars = sum(len(line) for line in rendered.splitlines()[1:])
        self.assertLessEqual(item_chars, RETRIEVAL_BUDGET_CHARS)
        foreign = retrieve_relevant_observations(
            [{"run_id": "x", "user_text": "aaaaaaaa bb",
              "assistant_text": "", "mode": "chat", "ts": ""}],
            "以后回答短一点")
        self.assertEqual(foreign, ())

    def test_project_scope_recalls_other_sessions_not_other_projects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            for session_id, project, run_id in (
                ("s1", "proj-a", "r1"),
                ("s2", "proj-a", "r2"),
                ("s1", "proj-b", "r3"),
            ):
                self.assertTrue(store.append_completed(
                    run_id=run_id, session_id=session_id, project=project,
                    mode="project", user_text="hi", assistant_text="hello",
                    stop_reason="done", provider_id="local"))
            same_project = store.read_committed(
                session_id="s9", project="proj-a", scope="project")
            self.assertEqual(
                sorted(str(row.get("run_id")) for row in same_project), ["r1", "r2"])
            session_only = store.read_committed(session_id="s1", project="proj-a")
            self.assertEqual(str(session_only[0].get("run_id")), "r1")
            user_all = store.read_committed(scope="user")
            self.assertEqual(len(user_all), 3)
            with self.assertRaises(ValueError):
                store.read_committed(session_id="s1", scope="project")
            with self.assertRaises(ValueError):
                store.read_committed(session_id="s1", scope="archive")

    def test_settlement_cost_at_observation_cap_completes(self) -> None:
        """Baseline: one settlement round (append + read + retrieve) against
        a full 5000-row store must finish well inside the faulthandler budget.
        The store is a bounded sync scan, not a background index."""
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            with store.database.connection() as database:
                database.executemany(
                    """
                    INSERT INTO observation_events(
                        run_id, session_id, project, mode, user_text,
                        assistant_text, stop_reason, committed, provider_id, ts
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            f"r{i}", "s", "", "chat", f"第{i}条 以后回答短一点", "好的",
                            "done", 1, "local", "2026-09-26T00:00:00Z",
                        )
                        for i in range(MAX_OBSERVATIONS)
                    ],
                )
            started = time.perf_counter()
            self.assertTrue(store.append_completed(
                run_id="r-new", session_id="s", mode="chat",
                user_text="以后回答短一点", assistant_text="好的",
                stop_reason="done", provider_id="local"))
            committed = store.read_committed(session_id="s")
            picked = retrieve_relevant_observations(committed, "以后回答短一点")
            elapsed = time.perf_counter() - started
            self.assertTrue(picked)
            self.assertLess(elapsed, 30.0)

    def test_overlong_first_item_is_truncated_within_budget(self) -> None:
        rows = [{
            "run_id": "r1",
            "user_text": "以后回答短一点" + "很长" * 2000,
            "assistant_text": "好的" * 2000,
            "mode": "chat",
            "ts": "2026-09-26T00:00:00Z",
        }]
        picked = retrieve_relevant_observations(rows, "以后回答短一点", budget_chars=1800)
        self.assertEqual([row["run_id"] for row in picked], ["r1"])
        rendered = render_retrieved_block(picked, 1800)
        item_chars = sum(len(line) for line in rendered.splitlines()[1:])
        self.assertLessEqual(item_chars, 1800)
        self.assertIn("…", rendered)

    def test_assistant_only_hit_shows_assistant_excerpt(self) -> None:
        rows = [{
            "run_id": "r1",
            "user_text": "嗯",
            "assistant_text": "以后回答短一点的详细方案如下",
            "mode": "chat",
            "ts": "2026-09-26T00:00:00Z",
        }]
        picked = retrieve_relevant_observations(rows, "以后回答短一点")
        self.assertEqual([row["run_id"] for row in picked], ["r1"])
        rendered = render_retrieved_block(picked, RETRIEVAL_BUDGET_CHARS)
        self.assertIn("以后回答短一点的详细方案如下", rendered)

    def test_three_item_cumulative_budget_holds(self) -> None:
        rows = [
            {"run_id": f"r{i}",
             "user_text": f"以后回答短一点第{i}条" + "内容" * 400,
             "assistant_text": "好的" + "补充" * 400,
             "mode": "chat", "ts": "2026-09-26T00:00:00Z"}
            for i in range(5)
        ]
        picked = retrieve_relevant_observations(rows, "以后回答短一点", budget_chars=1800)
        self.assertLessEqual(len(picked), MAX_RETRIEVED_ITEMS)
        rendered = render_retrieved_block(picked, 1800)
        item_chars = sum(len(line) for line in rendered.splitlines()[1:])
        self.assertLessEqual(item_chars, 1800)


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
                project_text="",
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
                project_text="/repo",
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
                project_text="",
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
                    project_text="",
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
