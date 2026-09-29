"""Forged workspace proof sources and payloads must be rejected.

Locks P1 proof unforgeability:
- any object with ``trusted=True`` alone is not a proof
- ``TrustedWorkspaceProof`` with an unknown source is rejected
- persisted frame payloads without durable verification stay untrusted
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

LEGIT_FP = "sha256:" + "ab" * 32
FORGED_FP = "sha256:" + "0" * 64


def _call(name="read_file"):
    from codey.runtime.core.models import ToolCall

    args = {"path": "a.py"} if name != "edit" else {"path": "a.py", "content": "x\n"}
    return ToolCall(name=name, args=args, call_id="c1")


class TrustedProofSourceAllowlistTests(unittest.TestCase):
    def test_forged_identity_payload_is_rejected(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import TrustedWorkspaceProof, attach_trusted_workspace
        from codey.runtime.core.models import ToolResult

        class Forged:
            trusted = True
            revision = 999
            fingerprint = FORGED_FP

            def attach_to_audit(self, audit):
                out = dict(audit)
                out["workspace_revision"] = 999
                out["workspace_fingerprint"] = FORGED_FP
                return out

        result = ToolResult(call=_call(), model_text="content")
        with self.assertRaises(RecoveryFailed):
            attach_trusted_workspace(result, TrustedWorkspaceProof(identity=Forged(), source="bump_state"))

    def test_unknown_proof_source_is_rejected(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import TrustedWorkspaceProof, attach_trusted_workspace
        from codey.runtime.core.models import ToolResult
        from codey.workspace.revision import WorkspaceIdentity

        legit = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        result = ToolResult(call=_call(), model_text="content")
        with self.assertRaises(RecoveryFailed):
            attach_trusted_workspace(
                result, TrustedWorkspaceProof(identity=legit, source="attacker_source")
            )

    def test_persisted_frame_payload_requires_durable_verification(self) -> None:
        from codey.operations.kernel_provenance import _trusted_workspace_from_result
        from codey.operations.kernel_recovery_result import (
            build_recovered_result,
            spec_from_frame_row,
        )
        from codey.toolchain.runtime import ToolOutcome
        from codey.workspace.revision import WorkspaceIdentity

        legit = WorkspaceIdentity.trusted_pair(2, LEGIT_FP)
        call = _call("read_file")
        outcome = ToolOutcome("content", True, audit={})
        row = SimpleNamespace(
            call=call, outcome=outcome, turn=1, tool_index=0, workspace_identity=legit
        )
        # A raw persisted identity without a verified proof source must not
        # restore trust on its own; durable store verification owns that.
        result = build_recovered_result(spec_from_frame_row(row))
        rev, _fp = _trusted_workspace_from_result(result)
        self.assertEqual(rev, 0)


if __name__ == "__main__":
    unittest.main()
