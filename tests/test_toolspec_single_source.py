"""ToolSpec is the single parameter/grant source; safety stays at execution."""
from __future__ import annotations

import unittest


class ToolSpecSingleSourceTests(unittest.TestCase):
    def test_custom_tool_passes_via_spec_alone(self) -> None:
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp
        from codey.toolchain import tool_spec as ts

        name = "custom_demo_tool_xyz"
        try:
            ts.register_custom_tool(name, grant="control", parameters=(("msg", {"type": "string"}),), required=("msg",))
            policy = SimpleNamespace(allows=lambda g: True)
            # Missing required -> rejected via spec.
            plan = kp.normalize_turn(
                '{"tool":"' + name + '","args":{}}', policy=policy,
            )
            self.assertTrue(plan.protocol_error, plan)
            # Present required -> accepted via spec alone (no legacy repair).
            plan2 = kp.normalize_turn(
                '{"tool":"' + name + '","args":{"msg":"hi"}}', policy=policy,
            )
            self.assertEqual(plan2.protocol_error, "")
            self.assertEqual(len(plan2.calls), 1)
        finally:
            ts.unregister_custom_tool(name)

    def test_unknown_tool_denied_never_control(self) -> None:
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp

        policy = SimpleNamespace(allows=lambda g: True)
        plan = kp.normalize_turn('{"tool":"no_such_tool_xyz","args":{}}', policy=policy)
        self.assertEqual(plan.calls, [])
        self.assertTrue(plan.protocol_error)
        self.assertIsNone(plan.control)

    def test_project_path_safety_enforced_at_execution(self) -> None:
        # Shape passes via spec (path present), traversal blocked at execution.
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp

        policy = SimpleNamespace(allows=lambda g: True)
        plan = kp.normalize_turn('{"tool":"read_file","args":{"path":"../escape"}}', policy=policy)
        # Spec shape allows path-like strings; execution boundary must deny.
        # Here we only lock that protocol does not claim safety (it may pass
        # shape); the execution test below locks the deny.
        self.assertTrue(plan.calls or plan.protocol_error)
        import tempfile
        from pathlib import Path

        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            sess_policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
            session = TaskSession(policy=sess_policy, task_kind="project", project=td, max_turns=2)
            results = execute_turn(
                session, [ToolCall("read_file", {"path": "../escape"}, "c1")],
                executors={"read_file": lambda c: ToolResult(call=c, model_text="should not run")},
                run_id="r", turn=1, project_path=Path(td),
            )
            self.assertIn("ERROR", results[0].model_text)


if __name__ == "__main__":
    unittest.main()
