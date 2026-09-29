"""Frame legacy revision/fingerprint fields must never become trusted.

Locks P1 legacy fallback removal: sibling ``workspace_revision`` /
``workspace_fingerprint`` attributes beside audit are display only.
Only a verified ``TrustedWorkspaceProof`` (or nothing for safe replay)
may carry trust; legacy ints alone stay untrusted.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

FORGED_FP = "sha256:" + "0" * 64


def _safe_row(**extra):
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
    outcome = ToolOutcome("content", True, audit={})
    return SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0, **extra)


class FrameLegacyFieldsNeverTrustedTests(unittest.TestCase):
    def test_legacy_sibling_fields_stay_untrusted(self) -> None:
        from codey.operations.kernel_recovery_result import (
            build_recovered_result,
            spec_from_frame_row,
        )
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        row = _safe_row(workspace_revision=999, workspace_fingerprint=FORGED_FP)
        result = build_recovered_result(spec_from_frame_row(row))
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (0, ""))
        self.assertNotIn("workspace_revision", dict(result.audit))

    def test_legacy_fields_do_not_create_proof(self) -> None:
        from codey.operations.kernel_recovery_result import spec_from_frame_row

        row = _safe_row(workspace_revision=999, workspace_fingerprint=FORGED_FP)
        spec = spec_from_frame_row(row)
        self.assertIsNone(getattr(spec, "trusted_workspace", None))


if __name__ == "__main__":
    unittest.main()
