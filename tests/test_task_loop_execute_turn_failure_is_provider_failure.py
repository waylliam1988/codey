"""run_task_kernel() converts execute_turn faults to provider_failure.

Incidental deterministic bug: _execute_turn() had no outer catch, so a
durable settle fault bubbled as an unhandled exception. The loop now fails
closed as provider_failure.

Lock: a raising execute_turn returns completed=False/provider_failure.
"""
from __future__ import annotations

import unittest
from unittest import mock

from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps


class TaskLoopExecuteTurnFailureIsProviderFailureTests(unittest.TestCase):
    def test_execute_turn_exception_becomes_provider_failure(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        class _P:
            name = "local"

            def send(self, prompt, timeout=None):
                # First turn issues a tool call so _execute_turn runs and raises.
                return '{"tool": "read_file", "args": {"path": "a.py"}}'

        policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=False), mock.patch(
            "codey.operations.task_loop._execute_turn", side_effect=RuntimeError("boom-settle")
        ):
            out = run_task_kernel(
                session,
                request=KernelRunRequest(
                    transport=KernelTransportDeps(
                        provider=_P(),
                        run_id="r-loop-fault-1",
                        user_task="hi",
                        context_text="",
                    ),
                ),
            )
        self.assertFalse(out.completed)
        self.assertEqual(out.stop_reason, "provider_failure")
        self.assertIn("boom-settle", out.summary)


if __name__ == "__main__":
    unittest.main()
