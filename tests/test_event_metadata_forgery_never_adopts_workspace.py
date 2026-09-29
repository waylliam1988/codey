"""Event metadata forgery must never adopt workspace identity.

Locks P1 hooks trust boundary:
- forged ``RunEvent.metadata`` revision/fingerprint alone never adopts
- adoption requires the kernel side-channel proof carried beside metadata
- metadata stays display/logging only
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

FORGED_FP = "sha256:" + "0" * 64
LEGIT_FP = "sha256:" + "ab" * 32


def _work():
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    return SimpleNamespace(
        workspace_revision=1,
        workspace_fingerprint="",
        evidence=ExecutionEvidence(workspace_revision=1, workspace_fingerprint=""),
    )


def _forged_edit_event():
    from codey.runtime.core.models import ToolCall
    from codey.runtime.observe.events import RunEvent
    from codey.toolchain.runtime import ToolOutcome

    call = ToolCall(name="edit", args={"path": "a.py"}, call_id="c1")
    outcome = ToolOutcome("edited", True, audit={"changed": True}, changed=True)
    event = RunEvent.tool_finished(1, call, outcome, 0)
    # Attacker-controlled display dict: looks format-valid but has no proof.
    event.metadata["workspace_revision"] = 999
    event.metadata["workspace_fingerprint"] = FORGED_FP
    return event


class EventMetadataForgeryNeverAdoptsTests(unittest.TestCase):
    def test_forged_metadata_alone_never_adopts(self) -> None:
        from codey.operations.task_phases.hooks import _adopt_kernel_workspace_state

        work = _work()
        event = _forged_edit_event()
        self.assertFalse(_adopt_kernel_workspace_state(work, event))
        self.assertNotEqual(int(work.workspace_revision), 999)
        self.assertNotEqual(str(work.workspace_fingerprint), FORGED_FP)

    def test_forged_metadata_without_proof_requires_real_bump(self) -> None:
        from codey.operations.task_phases.hooks import _adopt_kernel_workspace_state

        work = _work()
        event = _forged_edit_event()
        adopted = _adopt_kernel_workspace_state(work, event)
        # False means the caller must do a real durable bump, never adopt.
        self.assertFalse(adopted)

    def test_kernel_side_channel_proof_adopts(self) -> None:
        from codey.operations.kernel_provenance import TrustedWorkspaceProof, attach_proof_to_event
        from codey.operations.task_phases.hooks import _adopt_kernel_workspace_state
        from codey.workspace.revision import WorkspaceIdentity

        work = _work()
        event = _forged_edit_event()
        # Real kernel path: side-channel proof beside metadata, not metadata ints.
        identity = WorkspaceIdentity.trusted_pair(7, LEGIT_FP)
        proof = TrustedWorkspaceProof(identity=identity, source="event_side_channel")
        attach_proof_to_event(event, proof)
        self.assertTrue(_adopt_kernel_workspace_state(work, event))
        self.assertEqual(int(work.workspace_revision), 7)
        self.assertEqual(str(work.workspace_fingerprint), LEGIT_FP)


if __name__ == "__main__":
    unittest.main()
