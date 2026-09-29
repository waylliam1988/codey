"""Frame recovery allows safe replay only; unsafe rows fail closed.

Locks P2 unsafe provenance: ``spec_from_frame_row`` must raise
``RecoveryFailed`` for edit/run/shell rows instead of building an
unprovenanced success. Safe reads/searches still rebuild.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


def _row(name):
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    args = {"path": "a.py", "content": "x\n"} if name == "edit" else (
        {"path": ".", "command": "echo hi"} if name in {"run", "shell"} else {"path": "a.py"}
    )
    call = ToolCall(name=name, args=args, call_id="c1")
    outcome = ToolOutcome("ok", True, audit={} if name != "edit" else {"changed": True},
                          changed=(name == "edit"))
    return SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0)


class FrameUnsafeToolsRejectedTests(unittest.TestCase):
    def test_edit_frame_row_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import spec_from_frame_row

        with self.assertRaises(RecoveryFailed):
            spec_from_frame_row(_row("edit"))

    def test_run_frame_row_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import spec_from_frame_row

        with self.assertRaises(RecoveryFailed):
            spec_from_frame_row(_row("run"))

    def test_shell_frame_row_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import spec_from_frame_row

        with self.assertRaises(RecoveryFailed):
            spec_from_frame_row(_row("shell"))

    def test_safe_frame_row_succeeds(self) -> None:
        from codey.operations.kernel_recovery_result import (
            build_recovered_result,
            spec_from_frame_row,
        )

        result = build_recovered_result(spec_from_frame_row(_row("read_file")))
        self.assertFalse(str(result.model_text or "").startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
