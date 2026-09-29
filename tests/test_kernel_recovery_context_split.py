"""Recovery context and persisted verification have one focused owner."""
from __future__ import annotations

from pathlib import Path


def test_recovery_context_owner_is_separate_from_slot_orchestration() -> None:
    root = Path(__file__).resolve().parents[1] / "codey" / "operations"
    context_source = (root / "kernel_recovery_context.py").read_text(encoding="utf-8")
    recovery_source = (root / "kernel_recovery.py").read_text(encoding="utf-8")

    assert "class RecoveryContext" in context_source
    assert "def verified_persisted_identity" in context_source
    assert "class RecoveryContext" not in recovery_source
    assert "def _verified_persisted_identity" not in recovery_source
