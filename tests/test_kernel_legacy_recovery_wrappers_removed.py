"""Legacy recovery wrappers have no production callers and stay deleted."""
from __future__ import annotations

import unittest
from pathlib import Path


class KernelLegacyRecoveryWrappersRemovedTests(unittest.TestCase):
    def test_legacy_recovery_wrappers_are_not_exported_or_defined(self) -> None:
        root = Path(__file__).resolve().parents[1]
        recovery = (root / "codey" / "operations" / "kernel_recovery.py").read_text(encoding="utf-8")
        provenance = (root / "codey" / "operations" / "kernel_provenance.py").read_text(encoding="utf-8")
        for name in ("_replay_settled_slot", "_delivered_slot_result"):
            self.assertNotIn(f"def {name}", recovery)
            self.assertNotIn(f'"{name}"', recovery)
        self.assertNotIn("def _trusted_workspace_from_result", provenance)
        self.assertNotIn('"_trusted_workspace_from_result"', provenance)


if __name__ == "__main__":
    unittest.main()
