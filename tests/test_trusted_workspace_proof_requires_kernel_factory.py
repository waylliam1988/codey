"""Trusted workspace proofs must be created only by the kernel factory."""
from __future__ import annotations

import unittest


class TrustedWorkspaceProofRequiresKernelFactoryTests(unittest.TestCase):
    def test_direct_proof_construction_is_rejected(self) -> None:
        from codey.operations.kernel_provenance import TrustedWorkspaceProof
        from codey.workspace.revision import WorkspaceIdentity

        with self.assertRaises(TypeError):
            TrustedWorkspaceProof(
                identity=WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32),
                source="bump_state",
            )


if __name__ == "__main__":
    unittest.main()
