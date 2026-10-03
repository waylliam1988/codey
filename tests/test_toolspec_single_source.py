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
                executors={"read_file": lambda c: ToolResult(ok=True, call=c, model_text="should not run")},
                run_id="r", turn=1, project_path=Path(td),
            )
            self.assertIn("ERROR", results[0].model_text)

    def test_migrated_native_permission_denied_via_policy(self) -> None:
        # Old codec permission scenario now via production normalize_turn.
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = SimpleNamespace(allows=lambda g: g != "project.write")
        reply = AssistantTurn(
            text="", tool_calls=(ProviderToolCall(id="c1", name="edit", arguments={"path": "a.py"}),)
        )
        plan = kp.normalize_turn(reply, policy=policy)
        self.assertTrue(plan.protocol_error)
        self.assertEqual(plan.calls, [])

    def test_migrated_native_bad_args_become_protocol_error(self) -> None:
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = SimpleNamespace(allows=lambda g: True)
        reply = AssistantTurn(
            text="", tool_calls=(ProviderToolCall(id="c2", name="read_file", arguments={"offset": "x"}),)
        )
        plan = kp.normalize_turn(reply, policy=policy)
        self.assertTrue(plan.protocol_error)
        self.assertEqual(plan.calls, [])

    def test_migrated_mixed_done_fails_whole_turn_both_orders(self) -> None:
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = SimpleNamespace(allows=lambda g: True)
        read = ProviderToolCall(id="r", name="read_file", arguments={"path": "app.py"})
        done = ProviderToolCall(id="d", name="done", arguments={"summary": "bye"})
        for calls in ((done, read), (read, done)):
            plan = kp.normalize_turn(AssistantTurn(text="", tool_calls=calls), policy=policy)
            self.assertTrue(plan.protocol_error, calls)
            self.assertEqual(plan.calls, [])
            self.assertIsNone(plan.control)

    def test_migrated_missing_call_id_fails_before_execution(self) -> None:
        from types import SimpleNamespace

        from codey.operations import kernel_protocol as kp
        from codey.providers.base import AssistantTurn, ProviderToolCall

        policy = SimpleNamespace(allows=lambda g: True)
        plan = kp.normalize_turn(
            AssistantTurn(
                text="", tool_calls=(ProviderToolCall(id="", name="read_file", arguments={"path": "app.py"}),)
            ),
            policy=policy,
        )
        self.assertTrue(plan.protocol_error)
        self.assertEqual(plan.calls, [])

    def test_migrated_native_call_ids_preserved_and_mixed_rejected(self) -> None:
        from codey.operations import kernel_transport as t
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        ok = ToolResult(ok=True, call=ToolCall(name="read_file", args={"path": "a"}, call_id="call_1"), model_text="hi")
        messages = t._native_tool_messages([ok], session)
        self.assertEqual(messages[0]["tool_call_id"], "call_1")
        missing = ToolResult(ok=True, call=ToolCall(name="read_file", args={"path": "b"}), model_text="hi")
        with self.assertRaises(ValueError):
            t._native_tool_messages([ok, missing], session)


if __name__ == "__main__":
    unittest.main()
