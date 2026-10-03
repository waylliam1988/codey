"""Durable settlement must fail closed when trusted provenance cannot be read."""
from __future__ import annotations

import unittest
from unittest import mock


class KernelSettlementProvenanceFailureTests(unittest.TestCase):
    def test_provenance_read_failure_does_not_write_success_receipt(self) -> None:
        from codey.operations import kernel_execution
        from codey.operations.kernel_errors import EffectSettlementFailed
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})))
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        result = ToolResult(ok=True, call=call, model_text="content")

        with mock.patch(
            "codey.operations.kernel_execution._kernel_workspace_identity_of",
            side_effect=RuntimeError("provenance channel unavailable"),
        ), self.assertRaises(EffectSettlementFailed):
            kernel_execution._settle_slot(session, "effect", call, result, ok=True)

        self.assertNotIn("effect", session.executed)


if __name__ == "__main__":
    unittest.main()
