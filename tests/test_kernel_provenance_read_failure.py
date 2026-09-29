"""Kernel provenance reads must distinguish absence from a broken side-channel."""
from __future__ import annotations

import unittest


class KernelProvenanceReadFailureTests(unittest.TestCase):
    def test_broken_workspace_side_channel_raises_recovery_failed(self) -> None:
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import (
            _KERNEL_WORKSPACE_ATTR,
            _kernel_workspace_identity_of,
        )

        class BrokenResult:
            def __getattribute__(self, name: str):
                if name == _KERNEL_WORKSPACE_ATTR:
                    raise RuntimeError("workspace side-channel unavailable")
                return super().__getattribute__(name)

        with self.assertRaises(RecoveryFailed):
            _kernel_workspace_identity_of(BrokenResult())


if __name__ == "__main__":
    unittest.main()
