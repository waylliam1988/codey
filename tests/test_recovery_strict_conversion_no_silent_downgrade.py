"""Recovery conversion must be strict: bad fields raise, never empty-default.

Locks P2 silent downgrade removal:
- present-but-unreadable audit/presentation/canonical/model_text raise
- present-but-wrong-type presentation/canonical raise
- missing fields use defaults only when truly absent
"""
from __future__ import annotations

import unittest
from unittest import mock


def _call():
    from codey.runtime.core.models import ToolCall

    return ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")


class RecoveryStrictConversionTests(unittest.TestCase):
    def test_bad_presentation_type_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import (
            RecoveredResultSpec,
            build_recovered_result,
        )

        spec = RecoveredResultSpec(call=_call(), model_text="ok", audit={}, presentation="bad-string")
        with self.assertRaises(RecoveryFailed):
            build_recovered_result(spec)

    def test_bad_canonical_type_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import (
            RecoveredResultSpec,
            build_recovered_result,
        )

        spec = RecoveredResultSpec(call=_call(), model_text="ok", audit={}, canonical="bad-string")
        with self.assertRaises(RecoveryFailed):
            build_recovered_result(spec)

    def test_bad_audit_mapping_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import (
            RecoveredResultSpec,
            build_recovered_result,
        )

        spec = RecoveredResultSpec(call=_call(), model_text="ok", audit="bad-string")
        with self.assertRaises(RecoveryFailed):
            build_recovered_result(spec)

    def test_memory_result_bad_presentation_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import spec_from_memory_result
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        stored = ToolResult(call=call, model_text="ok", audit={})
        # Corrupt the built presentation with a non-mapping that survives
        # __post_init__ projection as a dict? Force a bad attribute instead.
        object.__setattr__(stored, "presentation", "bad-string")
        with self.assertRaises(RecoveryFailed):
            spec_from_memory_result(stored, call)

    def test_memory_result_provenance_read_failure_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import spec_from_memory_result
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="edit", args={"path": "a.py"}, call_id="c1")
        stored = ToolResult(call=call, model_text="edited", audit={"changed": True})
        with mock.patch(
            "codey.operations.kernel_provenance._kernel_workspace_identity_of",
            side_effect=RuntimeError("provenance channel unavailable"),
        ), self.assertRaises(RecoveryFailed):
            spec_from_memory_result(stored, call)

    def test_missing_fields_use_defaults(self) -> None:
        from codey.operations.kernel_recovery_result import (
            RecoveredResultSpec,
            build_recovered_result,
        )

        spec = RecoveredResultSpec(call=_call(), model_text="ok", audit={})
        result = build_recovered_result(spec)
        self.assertEqual(dict(result.presentation), {})
        self.assertEqual(dict(result.canonical), {})


if __name__ == "__main__":
    unittest.main()
