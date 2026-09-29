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
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome
        from codey.workspace.revision import WorkspaceIdentity

        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        legit = WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32)
        # Display audit carries no provenance keys (sanitized); the trusted
        # pair travels in the kernel-owned payload beside audit.
        outcome = ToolOutcome(
            "edited",
            True,
            canonical={"path": "a.py"},
            presentation={"status": "ok"},
            audit={"changed": True},
            changed=True,
            truncated=True,
        )
        frame = SimpleNamespace(
            run_id="r-delivered-meta",
            recovered_tool_outcomes=(
                SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0, workspace_identity=legit),
            ),
        )
        delivered = delivered_from_frame(frame, effect_scope="task")
        self.assertEqual(len(delivered), 1)
        result = next(iter(delivered.values()))
        audit = dict(getattr(result, "audit", {}) or {})
        self.assertTrue(audit.get("changed") is True, f"audit lost changed: {audit!r}")
        # Verified payload restores authoritative display + side-channel.
        self.assertEqual(audit.get("workspace_revision"), 2, f"provenance lost: {audit!r}")
        self.assertEqual(audit.get("workspace_fingerprint"), "sha256:" + "ab" * 32)
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (2, "sha256:" + "ab" * 32))
        presentation = dict(getattr(result, "presentation", {}) or {})
        self.assertIn("status", presentation, f"presentation lost: {presentation!r}")
        canonical = dict(getattr(result, "canonical", {}) or {})
        self.assertEqual(canonical.get("path"), "a.py", f"canonical lost: {canonical!r}")
        self.assertTrue(bool(getattr(result, "truncated", False)), "truncated lost")

    def test_display_audit_provenance_is_sanitized_without_payload(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome

        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        outcome = ToolOutcome(
            "edited", True, audit={"changed": True, "workspace_revision": 999,
                                   "workspace_fingerprint": "sha256:" + "0" * 64},
            changed=True,
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
