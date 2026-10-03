"""ToolResult identity checks name, call_id, AND args digest.

Repro (P2): _consistent_tool_result() only checked name and call_id, so the
same name/id with a different path was accepted:
requested a.py, returned other.py -> accepted.

Lock: differing args become an explicit ERROR reusing the requested call id;
matching args still pass through.
"""
from __future__ import annotations

import unittest

from codey.runtime.core.models import ToolCall, ToolResult


class ToolResultCallArgsDigestMismatchTests(unittest.TestCase):
    def test_different_path_same_name_and_id_is_rejected(self) -> None:
        from codey.operations.kernel_result import _consistent_tool_result

        requested = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        produced = ToolResult(
            ok=True, call=ToolCall(name="read_file", args={"path": "other.py"}, call_id="c1"),
            model_text="rogue",
        )
        out = _consistent_tool_result(requested, produced)
        self.assertTrue(str(out.model_text).startswith("ERROR:"), out.model_text[:300])
        self.assertEqual(str(out.call.call_id), "c1")
        self.assertEqual(dict(out.call.args), {"path": "a.py"})

    def test_matching_args_pass_through(self) -> None:
        from codey.operations.kernel_result import _consistent_tool_result

        requested = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        produced = ToolResult(ok=True, call=requested, model_text="hello")
        out = _consistent_tool_result(requested, produced)
        self.assertEqual(str(out.model_text), "hello")

    def test_execute_turn_rejects_args_mismatch_without_losing_receipt(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)

        def _rogue(call: ToolCall):
            return ToolResult(
                ok=True, call=ToolCall(name="read_file", args={"path": "other.py"}, call_id="c1"),
                model_text="rogue content",
            )

        results = execute_turn(
            session,
            [ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")],
            executors={"read_file": _rogue},
            run_id="r-args-mismatch-1", turn=1,
            project_path=None, tool_fns=None, research_tools=None,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(str(results[0].model_text).startswith("ERROR:"))
        self.assertEqual(str(results[0].call.call_id), "c1")


if __name__ == "__main__":
    unittest.main()
