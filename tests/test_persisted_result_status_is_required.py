"""Persisted result adapters cannot invent status for an incomplete record."""
import pytest

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_recovery_result import spec_from_persisted_record
from codey.runtime.core.models import ToolCall


def test_missing_status_is_rejected_instead_of_defaulted():
    with pytest.raises(RecoveryFailed, match="ok.*boolean"):
        spec_from_persisted_record({"name": "read_file", "excerpt": "old"}, ToolCall("read_file", {}), verified_identity=None)


def test_explicit_failure_is_retained():
    spec = spec_from_persisted_record({"name": "read_file", "excerpt": "old", "ok": False}, ToolCall("read_file", {}), verified_identity=None)
    assert spec.ok is False
