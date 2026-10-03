"""Once constructed, the structured tool result status cannot be reassigned."""
from dataclasses import FrozenInstanceError

import pytest

from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("replacement", [False, "false", "true", 1])
def test_frozen_result_preserves_original_boolean_status(replacement):
    result = ToolResult(ToolCall("read_file", {}), "output", ok=True)
    with pytest.raises(FrozenInstanceError):
        result.ok = replacement
    assert result.ok is True
