"""Research change restores are indexed by explicit session, not best-effort."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from codey.research.pipeline import ResearchPipeline
from tests.app_state import make_app_state


class ResearchChangesSessionTests(unittest.TestCase):
    def test_pipeline_sink_receives_explicit_session_id(self) -> None:
        received: list[tuple] = []
        sentinel = object()
        pipeline = ResearchPipeline(
            context=SimpleNamespace(run_id="run-1", session_id="session-9"),
            run_iteration=mock.Mock(),
            search_factory=lambda: None,
            research_changes_sink=(
                lambda run_id, changes, session_id="": received.append(
                    (run_id, changes, session_id)
                )
            ),
        )

        pipeline._record_research_changes(SimpleNamespace(changes=sentinel))

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
