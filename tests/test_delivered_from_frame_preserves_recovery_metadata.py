"""delivered_from_frame must preserve full recovery metadata.

Repro: a recovered row carrying audit/presentation/canonical/truncated is
rebuilt by ``delivered_from_frame`` as a bare ``ToolResult(call,
model_text)``. The recovered edit therefore loses its audit and trusted
workspace side-channel, so downstream ``execute_turn -> event -> hooks``
cannot adopt and bumps again.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace


class DeliveredFromFramePreservesRecoveryMetadataTests(unittest.TestCase):
    def test_preserves_audit_presentation_canonical_truncated(self) -> None:
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome

        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        outcome = ToolOutcome(
            "edited",
            True,
            canonical={"path": "a.py"},
            presentation={"status": "ok"},
            audit={"changed": True, "workspace_revision": 2,
                   "workspace_fingerprint": "sha256:" + "ab" * 32},
            changed=True,
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
        self.assertTrue(audit.get("changed") is True, f"audit lost changed: {audit!r}")
        self.assertIn("workspace_revision", audit, f"audit lost provenance: {audit!r}")
        presentation = dict(getattr(result, "presentation", {}) or {})
        self.assertIn("status", presentation, f"presentation lost: {presentation!r}")
        canonical = dict(getattr(result, "canonical", {}) or {})
        self.assertEqual(canonical.get("path"), "a.py", f"canonical lost: {canonical!r}")
        self.assertTrue(bool(getattr(result, "truncated", False)), "truncated lost")


if __name__ == "__main__":
    unittest.main()
