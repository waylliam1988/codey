"""delivered_from_frame must preserve full recovery metadata (sanitized).

Repro: a recovered row carrying audit/presentation/canonical/truncated was
rebuilt as a bare ``ToolResult(call, model_text)``. The recovered edit lost
its display metadata. Trust boundary: display audit provenance keys are
sanitized; only the kernel-owned ``workspace_identity`` payload restores the
trusted side-channel (see ``test_delivered_from_frame_trust_boundary``).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


class DeliveredFromFramePreservesRecoveryMetadataTests(unittest.TestCase):
    def test_preserves_audit_presentation_canonical_truncated(self) -> None:
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        # Frame is safe-replay only and never carries trusted provenance;
        # display metadata is preserved sanitized, side-channel stays empty.
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        outcome = ToolOutcome(
            "content",
            True,
            canonical={"path": "a.py"},
            presentation={"status": "ok"},
            audit={"extra": "keep"},
            truncated=True,
        )
        frame = SimpleNamespace(
            run_id="r-delivered-meta",
            recovered_tool_outcomes=(
                SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0),
            ),
        )
        delivered = delivered_from_frame(frame, effect_scope="task")
        self.assertEqual(len(delivered), 1)
        result = next(iter(delivered.values()))
        audit = dict(getattr(result, "audit", {}) or {})
        self.assertEqual(audit.get("extra"), "keep", f"audit lost: {audit!r}")
        self.assertNotIn("workspace_revision", audit)
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (0, ""))
        presentation = dict(getattr(result, "presentation", {}) or {})
        self.assertIn("status", presentation, f"presentation lost: {presentation!r}")
        canonical = dict(getattr(result, "canonical", {}) or {})
        self.assertEqual(canonical.get("path"), "a.py", f"canonical lost: {canonical!r}")
        self.assertTrue(bool(getattr(result, "truncated", False)), "truncated lost")

    def test_display_audit_provenance_is_sanitized_without_payload(self) -> None:
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        outcome = ToolOutcome(
            "content", True, audit={"workspace_revision": 999,
                                    "workspace_fingerprint": "sha256:" + "0" * 64},
        )
        frame = SimpleNamespace(
            run_id="r-delivered-meta-sanitize",
            recovered_tool_outcomes=(SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0),),
        )
        delivered = delivered_from_frame(frame, effect_scope="task")
        result = next(iter(delivered.values()))
        audit = dict(getattr(result, "audit", {}) or {})
        self.assertNotIn("workspace_revision", audit)
        self.assertNotIn("workspace_fingerprint", audit)
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (0, ""))


if __name__ == "__main__":
    unittest.main()
