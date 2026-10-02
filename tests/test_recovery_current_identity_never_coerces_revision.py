"""The revision store must corroborate receipts without converting its types."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.operations.kernel_recovery_context import RecoveryContext, verified_persisted_identity
from codey.workspace.revision import WorkspaceState

FINGERPRINT = "sha256:" + "ab" * 32


@pytest.mark.parametrize("revision", [True, "1", 1.0])
@pytest.mark.parametrize("use_context", [False, True])
def test_invalid_current_revision_never_corroborates_a_valid_receipt(revision, use_context):
    store = Mock()
    store.current_state.return_value = SimpleNamespace(revision=revision, fingerprint=FINGERPRINT)
    context = RecoveryContext(project_path="project", revision_store=store) if use_context else None
    assert verified_persisted_identity(
        {"workspace_revision": 1, "workspace_fingerprint": FINGERPRINT},
        project_path="project", revision_store=store, recovery_ctx=context,
    ) is None


@pytest.mark.parametrize("revision", [True, "1", 1.0])
@pytest.mark.parametrize("invalid_at", ["initial", "latest", "both"])
def test_epoch_comparison_cannot_wash_an_invalid_revision(revision, invalid_at):
    valid = WorkspaceState(revision=1, fingerprint=FINGERPRINT)
    invalid = SimpleNamespace(revision=revision, fingerprint=FINGERPRINT)
    states = [
        invalid if invalid_at in {"initial", "both"} else valid,
        invalid if invalid_at in {"latest", "both"} else valid,
    ]
    store = Mock()
    store.current_state.side_effect = states
    context = RecoveryContext(project_path="project", revision_store=store)
    context.current_state()
    assert context.workspace_epoch_stable() is False


def test_two_failed_store_reads_do_not_prove_epoch_stability():
    store = Mock()
    store.current_state.side_effect = OSError("store unavailable")
    context = RecoveryContext(project_path="project", revision_store=store)
    assert context.current_state() is None
    assert context.workspace_epoch_stable() is False


def test_valid_current_identity_corroborates_receipt_and_stable_epoch():
    store = Mock()
    store.current_state.return_value = WorkspaceState(revision=1, fingerprint=FINGERPRINT)
    context = RecoveryContext(project_path="project", revision_store=store)
    assert verified_persisted_identity(
        {"workspace_revision": 1, "workspace_fingerprint": FINGERPRINT},
        recovery_ctx=context,
    ) is not None
    assert context.workspace_epoch_stable() is True
