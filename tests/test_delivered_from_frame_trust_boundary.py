"""delivered_from_frame trust boundary: audit display is never trusted.

Frame recovery is safe-replay only and never carries trusted provenance.
Locks the P1 regression where ``recovery.delivered_from_frame`` rebuilt
``WorkspaceIdentity`` from ``outcome.audit`` display keys or from the
``workspace_identity`` payload / legacy sibling fields::

    audit.workspace_revision = 999
    audit.workspace_fingerprint = sha256:000...

Trust matrix (locked here):

- executor/frame audit            -> display only, never trusted
- UI/event metadata               -> display only, never trusted
- frame workspace_identity/legacy -> display only, never trusted
- TaskSession.executed fields     -> verified via durable store only
- WorkspaceRevisionStore.bump     -> trusted
- durable ledger exact match      -> trusted
- kernel side-channel copy        -> trusted

``audit`` may be copied sanitized; frame never restores the side-channel.
Unsafe frame rows fail closed instead of building unprovenanced success.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

FORGED_REV = 999
FORGED_FP = "sha256:" + "0" * 64
LEGIT_FP = "sha256:" + "ab" * 32


def _safe_row(*, audit: dict, workspace_identity=None, turn: int = 1, index: int = 0):
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
    outcome = ToolOutcome("content", True, audit=dict(audit))
    row = SimpleNamespace(call=call, outcome=outcome, turn=turn, tool_index=index)
    if workspace_identity is not None:
        row.workspace_identity = workspace_identity
    return row


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

        row = _safe_row(audit={"workspace_revision": FORGED_REV, "workspace_fingerprint": FORGED_FP})
        delivered = delivered_from_frame(_frame_with(row), effect_scope="task")
        self.assertEqual(len(delivered), 1)
        result = next(iter(delivered.values()))
        # Sanitized: forged provenance keys are dropped from display audit;
        # untrusted display must never look like trusted state.
        audit = dict(getattr(result, "audit", {}) or {})
        self.assertNotIn("workspace_revision", audit)
        self.assertNotIn("workspace_fingerprint", audit)
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual(
            (rev, fp), (0, ""),
            f"forged audit must stay untrusted, got {(rev, fp)!r} audit={audit!r}",
        )

    def test_frame_payload_never_restores_trusted(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.recovery import delivered_from_frame
        from codey.workspace.revision import WorkspaceIdentity

        legit = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        self.assertTrue(legit.trusted)
        # Even a format-valid payload beside audit never restores trust on
        # its own; durable store verification owns unsafe provenance and
        # frame recovery is safe-only.
        row = _safe_row(audit={}, workspace_identity=legit)
        delivered = delivered_from_frame(_frame_with(row), effect_scope="task")
        result = next(iter(delivered.values()))
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (0, ""))

    def test_unsafe_frame_row_fails_closed(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.recovery import delivered_from_frame

        row = _edit_row(audit={"changed": True})
        with self.assertRaises(RecoveryFailed):
            delivered_from_frame(_frame_with(row), effect_scope="task")

    def test_all_recovery_entries_share_trust_rule(self) -> None:
        """Every recovery entry must refuse forged audit without kernel provenance."""
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.kernel_recovery import _delivered_slot_result
        from codey.operations.project_adapter import _recovered_result_for_row
        from codey.operations.recovery import delivered_from_frame
        from codey.runtime.core.models import ToolCall

        forged_audit = {"workspace_revision": FORGED_REV, "workspace_fingerprint": FORGED_FP}
        row = _safe_row(audit=forged_audit)

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

        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        bare = ToolResult(call=call, model_text="content", audit=dict(forged_audit))
        got = _delivered_slot_result({"slot": bare}, "slot", call)
        self.assertIsNotNone(got)
        assert got is not None
        rev, _fp = _trusted_workspace_from_result(got)
        self.assertEqual(rev, 0, "_delivered_slot_result must not trust forged audit")


if __name__ == "__main__":
    unittest.main()
