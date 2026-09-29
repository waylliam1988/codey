"""Run event exit codes must come from the same effect, never prior history."""
from __future__ import annotations

import unittest


class KernelEventExitCodeEffectLocalTests(unittest.TestCase):
    def test_run_without_exit_does_not_reuse_previous_verification(self) -> None:
        from codey.operations.kernel_events import _emit_tool_results
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.read", "control"})),
            task_kind="project",
            project="p",
            max_turns=4,
        )
        session.record_verification("previous", 1, True, exit_code=0)
        call = ToolCall(name="run", args={"command": "current", "path": "."}, call_id="c1")
        session.executed[turn_effect_id("run:task", 1, 0)] = {"ok": True}
        events = []

        _emit_tool_results(
            events.append,
            session,
            [ToolResult(call=call, model_text="current output")],
            run_id="run:task",
            turn=1,
        )

        self.assertIsNone(events[0].outcome.exit_code)


if __name__ == "__main__":
    unittest.main()
