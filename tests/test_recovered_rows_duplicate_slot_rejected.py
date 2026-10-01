"""Duplicate or illegal turn/index slots must fail closed.

Locks P2 slot identity:
- duplicate ``(turn, tool_index)`` raises ``RecoveryFailed`` (no silent overwrite)
- ``bool`` turn/index rejected (``int(True)==1`` must not pass)
- negative turn/index rejected
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


def _row(turn, index, name="read_file"):
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    call = ToolCall(name=name, args={"path": "a.py"}, call_id=f"c{turn}-{index}")
    outcome = ToolOutcome("content", True, audit={})
    return SimpleNamespace(call=call, outcome=outcome, turn=turn, tool_index=index)


class RecoveredRowsDuplicateSlotRejectedTests(unittest.TestCase):
    def test_duplicate_slot_raises_in_frame(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.recovery import delivered_from_frame

        frame = SimpleNamespace(
            run_id="r-dup-1", recovered_tool_outcomes=(_row(1, 0), _row(1, 0))
        )
        with self.assertRaises(RecoveryFailed):
            delivered_from_frame(frame, effect_scope="task")

    def test_duplicate_slot_raises_in_entry_validation(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_session_recovery import _validate_recovered_rows

        with self.assertRaises(RecoveryFailed):
            _validate_recovered_rows([_row(1, 0), _row(1, 0)])

    def test_bool_turn_rejected(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_session_recovery import _validate_recovered_rows

        with self.assertRaises(RecoveryFailed):
            _validate_recovered_rows([_row(True, 0)])

    def test_negative_index_rejected(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_session_recovery import _validate_recovered_rows

        with self.assertRaises(RecoveryFailed):
            _validate_recovered_rows([_row(1, -1)])

    def test_negative_turn_rejected_in_frame(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.recovery import delivered_from_frame

        frame = SimpleNamespace(run_id="r-dup-2", recovered_tool_outcomes=(_row(-1, 0),))
        with self.assertRaises(RecoveryFailed):
            delivered_from_frame(frame, effect_scope="task")


if __name__ == "__main__":
    unittest.main()
