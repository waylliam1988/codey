"""Single recovery-result protocol: builder + sanitize + proof.

First layer of the unified protocol (see kernel_recovery_result):

- display metadata preserved, provenance keys sanitized
- no proof stays untrusted, legit proof restores trusted
- require_workspace_provenance without proof raises RecoveryFailed
- construction failure never returns bare success
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

LEGIT_FP = "sha256:" + "ab" * 32
FORGED_FP = "sha256:" + "0" * 64


def _call():
    from codey.runtime.core.models import ToolCall

    return ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")


class KernelRecoveryResultProtocolTests(unittest.TestCase):
    def test_sanitize_drops_provenance_keeps_display(self) -> None:
        from codey.operations.kernel_recovery_result import sanitize_recovery_audit

        cleaned = sanitize_recovery_audit(
            {"changed": True, "extra": "keep", "workspace_revision": 999,
             "workspace_fingerprint": FORGED_FP, "_kernel_workspace_trusted": True}
        )
        self.assertTrue(cleaned.get("changed") is True)
        self.assertEqual(cleaned.get("extra"), "keep")
        self.assertNotIn("workspace_revision", cleaned)
        self.assertNotIn("workspace_fingerprint", cleaned)
        self.assertNotIn("_kernel_workspace_trusted", cleaned)

    def test_builder_preserves_metadata_without_proof(self) -> None:
        from codey.operations.kernel_recovery_result import RecoveredResultSpec, build_recovered_result
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        spec = RecoveredResultSpec(
            call=_call(), model_text="edited",
            audit={"changed": True, "extra": "keep"},
            presentation={"status": "ok"}, canonical={"path": "a.py"}, truncated=True,
        )
        result = build_recovered_result(spec)
        self.assertEqual(dict(result.audit).get("changed"), True)
        self.assertEqual(dict(result.presentation).get("status"), "ok")
        self.assertEqual(dict(result.canonical).get("path"), "a.py")
        self.assertTrue(bool(result.truncated))
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (0, ""))

    def test_builder_restores_verified_proof(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_proof
        from codey.operations.kernel_recovery_result import RecoveredResultSpec, build_recovered_result
        from codey.workspace.revision import WorkspaceIdentity
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        ident = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        proof = _trusted_workspace_proof(ident, "bump_state")
        spec = RecoveredResultSpec(call=_call(), model_text="edited", audit={"changed": True}, trusted_workspace=proof)
        result = build_recovered_result(spec)
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (2, LEGIT_FP))
        audit = dict(result.audit)
        self.assertEqual(audit.get("workspace_revision"), 2)
        self.assertEqual(audit.get("workspace_fingerprint"), LEGIT_FP)

    def test_require_provenance_without_proof_raises(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_recovery_result import RecoveredResultSpec, build_recovered_result

        spec = RecoveredResultSpec(call=_call(), model_text="edited", audit={"changed": True},
                                   require_workspace_provenance=True)
        with self.assertRaises(RecoveryFailed) as ctx:
            build_recovered_result(spec)
        self.assertIn("provenance", str(ctx.exception).lower())

    def test_unverified_proof_rejected(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import _trusted_workspace_proof
        from codey.operations.kernel_recovery_result import RecoveredResultSpec, build_recovered_result
        from codey.workspace.revision import WorkspaceIdentity

        # Untrusted identity (0,"") must not attach even with a source.
        bad = _trusted_workspace_proof(WorkspaceIdentity(), "bump_state")
        with self.assertRaises(RecoveryFailed):
            build_recovered_result(RecoveredResultSpec(call=_call(), audit={}, trusted_workspace=bad))
        # Missing source must not attach even with trusted identity.
        legit = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        no_source = _trusted_workspace_proof(legit, "")
        with self.assertRaises(RecoveryFailed):
            build_recovered_result(RecoveredResultSpec(call=_call(), audit={}, trusted_workspace=no_source))

    def test_frame_row_forged_audit_stays_untrusted(self) -> None:
        from codey.operations.kernel_recovery_result import build_recovered_result, spec_from_frame_row
        from codey.runtime.core.models import ToolCall
        from codey.toolchain.runtime import ToolOutcome
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        # Frame is safe-replay only; forged audit on a safe read stays untrusted.
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        outcome = ToolOutcome("content", True, audit={"workspace_revision": 999,
                                                      "workspace_fingerprint": FORGED_FP})
        row = SimpleNamespace(call=call, outcome=outcome, turn=1, tool_index=0)
        result = build_recovered_result(spec_from_frame_row(row))
        rev, fp = _trusted_workspace_from_result(result)
        self.assertEqual((rev, fp), (0, ""))
        self.assertNotIn("workspace_revision", dict(result.audit))

    def test_recovery_audit_never_promotes_to_trusted(self) -> None:
        """Regression lock across all entries with the same forged audit."""
        from types import SimpleNamespace as NS

        from codey.operations.kernel_recovery_result import build_recovered_result, spec_from_frame_row
        from codey.operations.kernel_session_recovery import restore_task_session
        from codey.operations.recovery import delivered_from_frame
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.toolchain.runtime import ToolOutcome
        from tests.recovery_test_helpers import (
            delivered_slot_result as _delivered_slot_result,
        )
        from tests.recovery_test_helpers import (
            trusted_workspace_pair as _trusted_workspace_from_result,
        )

        forged = {"workspace_revision": 999, "workspace_fingerprint": FORGED_FP}
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        outcome = ToolOutcome("content", True, audit=dict(forged))
        row = NS(call=call, outcome=outcome, turn=1, tool_index=0)

        for result in (
            build_recovered_result(spec_from_frame_row(row)),
            restore_task_session(SimpleNamespace(run_id="r-x", recovered_tool_outcomes=(row,)), TaskSession(policy=TaskPolicy(grants=frozenset({"control"}))))[3][0],
            next(iter(delivered_from_frame(NS(run_id="r-x", recovered_tool_outcomes=(row,)), effect_scope="task").values())),
        ):
            rev, _fp = _trusted_workspace_from_result(result)
            self.assertEqual(rev, 0, f"audit must never promote: {dict(result.audit)!r}")

        bare = ToolResult(call=call, model_text="content", audit=dict(forged))
        got = _delivered_slot_result({"s": bare}, "s", call)
        assert got is not None
        rev, _fp = _trusted_workspace_from_result(got)
        self.assertEqual(rev, 0)


if __name__ == "__main__":
    unittest.main()
