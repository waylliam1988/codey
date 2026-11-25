"""Research change restores are indexed by explicit session, not best-effort."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from codey.research.context import ResearchContext
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.run_result import ResearchRunResult
from tests.app_state import make_app_state


def _allow_policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({
        "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


class ResearchChangesSessionTests(unittest.TestCase):
    def test_pipeline_sink_receives_explicit_session_id(self) -> None:
        received: list[tuple[str, object, str]] = []
        sentinel = object()
        result = ResearchRunResult("research", "result", "done", 1)
        writer = mock.Mock(return_value=ResearchIterationRun(result, SimpleNamespace(changes=sentinel)))
        pipeline = ResearchPipeline(
            context=ResearchContext(question="research", run_id="run-1", session_id="session-9", max_turns=1),
            run_iteration=writer,
            search_factory=lambda: None,
            research_changes_sink=(
                lambda run_id, changes, session_id="": received.append(
                    (run_id, changes, session_id)
                )
            ),
        )

        assert pipeline.run().final_result == result
        writer.assert_called_once()

        self.assertEqual(len(received), 1)
        run_id, changes, session_id = received[0]
        self.assertEqual(run_id, "run-1")
        self.assertIs(changes, sentinel)
        self.assertEqual(session_id, "session-9")

    def test_explicit_session_record_is_forgettable_without_active_run(self) -> None:
        state = make_app_state(self)
        state.record_research_changes("run-x", object(), session_id="session-x")

        failures = state.forget_conversation("session-x")

        self.assertNotIn("approvals", failures)
        self.assertNotIn("run-x", state.research_changes)


if __name__ == "__main__":
    unittest.main()
