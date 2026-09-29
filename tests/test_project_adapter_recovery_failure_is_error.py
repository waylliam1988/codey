"""Project adapter recovery failure must be an explicit error.

Repro: ``_recovered_result_for_row`` falls back to a half-recovered
``ToolResult(call, model_text, audit)`` that keeps the audit display but
drops presentation/canonical/truncated and the kernel side-channel. The
half result looks like a success with provenance.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


class ProjectAdapterRecoveryFailureIsErrorTests(unittest.TestCase):
    def test_builder_failure_raises_recovery_failed(self) -> None:
        from codey.operations import project_adapter as pa
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        outcome = ToolOutcome("content", True, audit={})
        row = SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0)
        with mock.patch(
            "codey.operations.kernel_result.build_recovered_tool_result",
            side_effect=RuntimeError("builder boom"),
        ):
            with self.assertRaises(RecoveryFailed) as ctx:
                pa._recovered_result_for_row(row)
            self.assertTrue(
                any(token in str(ctx.exception).lower() for token in ("recovered", "rebuild", "provenance")),
                f"RecoveryFailed must mention recovered/rebuild/provenance, got {ctx.exception!r}",
            )


if __name__ == "__main__":
    unittest.main()
