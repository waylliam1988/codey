"""Event proof adoption requires the kernel-only construction capability."""
from __future__ import annotations

import unittest


class EventProofRequiresKernelCapabilityTests(unittest.TestCase):
    def test_object_new_proof_without_capability_is_rejected(self) -> None:
        from codey.operations.kernel_provenance import TrustedWorkspaceProof, event_proof
        from codey.runtime.observe.events import RunEvent
        from codey.workspace.revision import WorkspaceIdentity

        event = RunEvent.info("x")
        proof = object.__new__(TrustedWorkspaceProof)
        object.__setattr__(proof, "identity", WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32))
        object.__setattr__(proof, "source", "event_side_channel")
        object.__setattr__(event, "_kernel_workspace_proof", proof)

        from codey.operations.kernel_errors import RecoveryFailed

        with self.assertRaises(RecoveryFailed):
            event_proof(event)

    def test_wrong_capability_is_rejected(self) -> None:
        from codey.operations.kernel_provenance import (
            _EVENT_PROOF_ATTR,
            TrustedWorkspaceProof,
            event_proof,
        )
        from codey.runtime.observe.events import RunEvent
        from codey.workspace.revision import WorkspaceIdentity

        event = RunEvent.info("x")
        proof = object.__new__(TrustedWorkspaceProof)
        object.__setattr__(proof, "identity", WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32))
        object.__setattr__(proof, "source", "event_side_channel")
        object.__setattr__(proof, "_capability", object())
        object.__setattr__(event, _EVENT_PROOF_ATTR, proof)

        from codey.operations.kernel_errors import RecoveryFailed

        with self.assertRaises(RecoveryFailed):
            event_proof(event)

    def test_forged_identity_is_rejected_even_with_kernel_capability(self) -> None:
        from codey.operations.kernel_provenance import (
            _EVENT_PROOF_ATTR,
            _trusted_workspace_proof,
            event_proof,
        )
        from codey.runtime.observe.events import RunEvent

        class Forged:
            trusted = True
            revision = 999
            fingerprint = "sha256:" + "ff" * 32

        event = RunEvent.info("x")
        object.__setattr__(event, _EVENT_PROOF_ATTR, _trusted_workspace_proof(Forged(), "event_side_channel"))

        from codey.operations.kernel_errors import RecoveryFailed

        with self.assertRaises(RecoveryFailed):
            event_proof(event)


if __name__ == "__main__":
    unittest.main()
