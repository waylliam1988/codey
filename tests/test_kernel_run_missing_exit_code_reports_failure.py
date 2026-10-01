"""Run without a structured exit must report failure on every projection.

Same missing-exit run is checked across: execution record -> RunEvent ->
UI payload -> headless payload -> session receipt. ok must be False,
no exit_code may be invented, output text is preserved, and no passing
verification is recorded.
"""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.read", "project.write", "project.verify", "control"}))


class MissingExitCodeReportsFailureTests(unittest.TestCase):
    def _run_once(self):
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="", max_turns=2)

        def fake(call):
            return ToolResult(call=call, model_text="run output", audit={})

        results = ke.execute_turn(
            session,
            [ToolCall(name="run", args={"command": "echo hi", "path": "."})],
            executors={"run": fake},
            run_id="r-missing-exit", turn=1,
        )
        return session, results[0]

    def test_missing_exit_is_failure_everywhere(self) -> None:
        from codey.app import headless_runner as hr
        from codey.operations import kernel_events as kev
        from codey.operations.kernel_result import result_ok
        from codey.runtime.observe.events import run_event_ui_payload

        session, result = self._run_once()
        self.assertFalse(result_ok("run", result))
        identity = next(iter(session.executed))
        self.assertFalse(session.executed[identity].get("ok", True))
        self.assertNotIn("exit_code", session.executed[identity])
        self.assertEqual(len(session.verifications), 1)
        self.assertFalse(session.verifications[0]["passed"])
        self.assertNotIn("exit_code", session.verifications[0])
        self.assertEqual(result.model_text, "run output")

        events: list = []
        kev._emit_tool_results(events.append, session, [result], run_id="r-missing-exit", turn=1)
        event = events[0]
        self.assertFalse(event.outcome.ok)
        self.assertIsNone(event.outcome.exit_code)
        payload = run_event_ui_payload("r", "s", event)
        assert payload is not None
        self.assertNotIn("exit_code", payload)
        self.assertFalse(payload.get("ok", True))
        headless = hr._payload_tool({"run_id": "r", "session_id": "s"}, payload)
        self.assertNotIn("exit_code", headless)
        self.assertFalse(headless.get("ok", True))


if __name__ == "__main__":
    unittest.main()
