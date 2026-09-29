"""delivered_from_frame trust boundary: audit display is never trusted.

Locks the P1 regression where ``recovery.delivered_from_frame`` rebuilt
``WorkspaceIdentity`` from ``outcome.audit`` display keys::

    audit.workspace_revision = 999
    audit.workspace_fingerprint = sha256:000...

and attached it as the trusted kernel side-channel, bypassing the
persisted-provenance verification.

Trust matrix (locked here):

- executor/frame audit            -> display only, never trusted
- UI/event metadata               -> display only, never trusted
- TaskSession.executed fields     -> verified via durable store only
- WorkspaceRevisionStore.bump     -> trusted
- durable ledger exact match      -> trusted
- kernel side-channel copy        -> trusted
- RecoveredToolOutcome.workspace_identity (kernel-owned) -> trusted

``audit`` may be copied; trusted provenance may only be copied after
verification.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

FORGED_REV = 999
FORGED_FP = "sha256:" + "0" * 64
LEGIT_FP = "sha256:" + "ab" * 32


def _edit_row(*, audit: dict, workspace_identity=None, turn: int = 1, index: int = 0):
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
    outcome = ToolOutcome("edited", True, audit=dict(audit), changed=True)
    row = SimpleNamespace(call=call, outcome=outcome, turn=turn, tool_index=index)
    if workspace_identity is not None:
        row.workspace_identity = workspace_identity
    return row


def _frame_with(*rows, run_id: str = "r-trust-boundary"):
    return SimpleNamespace(run_id=run_id, recovered_tool_outcomes=tuple(rows))


class DeliveredFromFrameTrustBoundaryTests(unittest.TestCase):
    def test_forged_audit_does_not_become_trusted(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.recovery import delivered_from_frame

        row = _edit_row(audit={"changed": True, "workspace_revision": FORGED_REV, "workspace_fingerprint": FORGED_FP})
        delivered = delivered_from_frame(_frame_with(row), effect_scope="task")
        self.assertEqual(len(delivered), 1)
        result = next(iter(delivered.values()))
        # Sanitized: forged provenance keys are dropped from display audit;
        # untrusted display must never look like trusted state.
        audit = dict(getattr(result, "audit", {}) or {})
        self.assertNotIn("workspace_revision", audit)
        self.assertNotIn("workspace_fingerprint", audit)
        self.assertTrue(audit.get("changed") is True)
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual(
            (rev, fp), (0, ""),
            f"forged audit must stay untrusted, got {(rev, fp)!r} audit={audit!r}",
        )

    def test_kernel_owned_provenance_restores_trusted(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.recovery import delivered_from_frame
        from codey.workspace.revision import WorkspaceIdentity

        legit = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        self.assertTrue(legit.trusted)
        # Legitimate provenance travels in the kernel-owned field, not audit.
        # Audit here carries no provenance keys at all.
        row = _edit_row(audit={"changed": True}, workspace_identity=legit)
        delivered = delivered_from_frame(_frame_with(row), effect_scope="task")
        result = next(iter(delivered.values()))
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (2, LEGIT_FP))

    def test_audit_tampering_does_not_change_trusted_identity(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.recovery import delivered_from_frame
        from codey.workspace.revision import WorkspaceIdentity

        legit = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        row = _edit_row(
            audit={"changed": True, "workspace_revision": FORGED_REV, "workspace_fingerprint": FORGED_FP},
            workspace_identity=legit,
        )
        delivered = delivered_from_frame(_frame_with(row), effect_scope="task")
        result = next(iter(delivered.values()))
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (2, LEGIT_FP), "tampered audit must not override kernel provenance")
        audit = dict(getattr(result, "audit", {}) or {})
        self.assertNotEqual(audit.get("workspace_revision"), FORGED_REV)
        self.assertEqual(audit.get("workspace_revision"), 2)
        self.assertEqual(audit.get("workspace_fingerprint"), LEGIT_FP)

    def test_all_recovery_entries_share_trust_rule(self) -> None:
        """Every recovery entry must refuse forged audit without kernel provenance."""
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.kernel_recovery import _delivered_slot_result
        from codey.operations.project_adapter import _recovered_result_for_row
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall

        forged_audit = {"changed": True, "workspace_revision": FORGED_REV, "workspace_fingerprint": FORGED_FP}
        row = _edit_row(audit=forged_audit)

        # 1) delivered_from_frame
        delivered = delivered_from_frame(_frame_with(row), effect_scope="task")
        rev, _fp = _trusted_workspace_from_result(next(iter(delivered.values())))
        self.assertEqual(rev, 0, "delivered_from_frame must not trust forged audit")

        # 2) project_adapter row rebuild keeps display but stays untrusted
        adapted = _recovered_result_for_row(row)
        rev, _fp = _trusted_workspace_from_result(adapted)
        self.assertEqual(rev, 0, "project_adapter must not trust forged audit")

        # 3) delivered slot rebuild stays untrusted (needs side-channel, not audit)
        from codey.runtime.core.models import ToolResult

        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        bare = ToolResult(call=call, model_text="edited", audit=dict(forged_audit))
        got = _delivered_slot_result({"slot": bare}, "slot", call)
        self.assertIsNotNone(got)
        assert got is not None
        rev, _fp = _trusted_workspace_from_result(got)
        self.assertEqual(rev, 0, "_delivered_slot_result must not trust forged audit")


if __name__ == "__main__":
    unittest.main()
