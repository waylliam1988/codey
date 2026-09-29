"""Explicit executor is not run when delegate policy check raises.

Repro: delegate ``handles()`` is True and ``_policy_check()`` raises.
The kernel must fail closed with an explicit ERROR result and must never
invoke the injected executor.

Lock: executor call count stays zero; result text starts with ERROR:.
"""
from __future__ import annotations

import unittest
from unittest import mock


class ExplicitExecutorPolicyCheckExceptionFailClosedTests(unittest.TestCase):
    def test_policy_check_exception_blocks_executor(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.read", "project.write", "control"})),
            task_kind="project", project="", max_turns=2,
        )
        called: list[int] = []

        def fake(call: ToolCall):
            called.append(1)
            return ToolResult(call=call, model_text="ok")

        class _ExplodingDelegate:
            def handles(self, name: str) -> bool:
                return True

            def _policy_check(self, call):
                raise RuntimeError("policy store boom")

        with mock.patch(
            "codey.operations.kernel_execution._build_delegate",
            return_value=_ExplodingDelegate(),
        ):
            results = ke.execute_turn(
                session,
                [ToolCall(name="read_file", args={"path": "a.py"})],
                executors={"read_file": fake},
                run_id="r-policy-exc-1", turn=1,
                project_path="/tmp",
            )
        self.assertEqual(called, [], "executor must not run when policy check raises")
        self.assertEqual(len(results), 1)
        self.assertTrue(
            str(results[0].model_text or "").startswith("ERROR:"),
            f"policy failure must be explicit error: {results[0].model_text!r}",
        )
        identity = next(iter(session.executed))
        self.assertFalse(session.executed[identity].get("ok", True))


if __name__ == "__main__":
    unittest.main()
