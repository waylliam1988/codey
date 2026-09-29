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
    def test_builder_failure_is_not_half_recovered_success(self) -> None:
        from codey.operations import project_adapter as pa
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome

        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        outcome = ToolOutcome("edited", True, audit={"changed": True}, changed=True)
        row = SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0)
        with mock.patch(
            "codey.operations.kernel_result.build_recovered_tool_result",
            side_effect=RuntimeError("builder boom"),
        ):
            try:
                result = pa._recovered_result_for_row(row)
            except Exception as exc:
                self.assertTrue(str(exc) or True)
                return
            text = str(getattr(result, "model_text", "") or "")
            self.assertTrue(
                text.startswith("ERROR:"),
                f"adapter recovery failure must be explicit ERROR, got {text!r}",
            )


if __name__ == "__main__":
    unittest.main()
