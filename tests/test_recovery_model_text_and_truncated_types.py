"""Recovered text fields reject present values with the wrong type."""
from __future__ import annotations

import unittest


class RecoveryModelTextAndTruncatedTypesTests(unittest.TestCase):
    def _spec(self, **kwargs):
        from codey.operations.kernel_recovery_result import RecoveredResultSpec
        from codey.runtime.core.models import ToolCall

        return RecoveredResultSpec(
            ok=True, call=ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"),
            audit={},
            **kwargs,
        )

    def test_present_non_string_model_text_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import build_recovered_result

        with self.assertRaises(RecoveryFailed):
            build_recovered_result(self._spec(model_text=0))

    def test_present_non_boolean_truncated_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import build_recovered_result

        with self.assertRaises(RecoveryFailed):
            build_recovered_result(self._spec(model_text="ok", truncated="false"))


if __name__ == "__main__":
    unittest.main()
