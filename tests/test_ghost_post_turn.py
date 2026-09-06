from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from codey.operations.ghost_post_turn import (
    GhostTaskPolicyDeps,
    maybe_sync_continuity,
)
from codey.task.model import TaskSubmission


class _State:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.ghost_continuity = mock.Mock(
            sync_from_sources=mock.Mock(side_effect=RuntimeError("secret path")),
        )
        self.ghost_hebbian = None
        self.ghost_inbox = None

    def emit(self, event: dict[str, object]) -> None:
        self.events.append(dict(event))


class GhostPostTurnTests(unittest.TestCase):
    def test_continuity_failure_records_sanitized_warning(self) -> None:
        state = _State()
        frame = SimpleNamespace(
            request=TaskSubmission(
                session_id="session-1",
                project=None,
                task="remember this",
                max_turns=4,
                continue_task=False,
                provider_id="deepseek",
            ),
            run_id="run-1",
            project_text="",
        )

        maybe_sync_continuity(
            GhostTaskPolicyDeps(state=state),
            frame,  # type: ignore[arg-type]
            {
                "type": "task_done",
                "mode": "chat",
                "run_id": "run-1",
                "session_id": "session-1",
                "stop_reason": "done",
            },
        )

        self.assertEqual(len(state.events), 1)
        warning = state.events[0]
        self.assertEqual(warning["type"], "ghost_post_turn_warning")
        self.assertEqual(warning["stage"], "continuity_sync")
        self.assertEqual(warning["run_id"], "run-1")
        self.assertEqual(warning["session_id"], "session-1")
        self.assertEqual(warning["error_type"], "RuntimeError")
        self.assertTrue(str(warning["error_ref"]).startswith("sha256:"))
        self.assertNotIn("secret path", str(warning))


if __name__ == "__main__":
    unittest.main()
