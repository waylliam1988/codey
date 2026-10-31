"""Recovery rebuild errors never create partially trusted tool results."""
from unittest import mock

import pytest

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_recovery_result import RecoveredResultSpec, build_recovered_result
from codey.runtime.core.models import ToolCall


def test_builder_failure_raises_recovery_failed():
    spec = RecoveredResultSpec(ToolCall("read_file", {"path": "a.py"}, call_id="c1"),
                               ok=True, model_text="content")
    with mock.patch("codey.operations.kernel_result.build_recovered_tool_result",
                    side_effect=RuntimeError("builder boom")), pytest.raises(RecoveryFailed, match="rebuild"):
        build_recovered_result(spec)
