"""Recovered redelivered marker must be an exact bool.

'false', 'true', 0, 1, None, [], {} must raise RecoveryFailed with a
boolean diagnostic, not route to settled redelivery. True redelivers the
original receipt without re-execution; False stays on safe replay.
"""
from __future__ import annotations

import pytest


def _row(redelivered):
    from types import SimpleNamespace

    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    return SimpleNamespace(
        call=ToolCall("read_file", {"path": "a.py"}, "c1"),
        outcome=ToolOutcome(model_text="ok", ok=True),
        turn=0,
        tool_index=0,
        redelivered=redelivered,
    )


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_redelivery_marker_requires_exact_bool(value):
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_recovery_result import spec_for_recovered_row

    row = _row(value)
    with pytest.raises(RecoveryFailed, match="redelivered.*boolean"):
        spec_for_recovered_row(row)


def test_true_redelivers_without_reexecution():
    from types import SimpleNamespace

    from codey.operations.kernel_recovery_result import spec_for_recovered_row
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    row = SimpleNamespace(
        call=ToolCall("edit", {"path": "a.py"}, "c1"),
        outcome=ToolOutcome(model_text="edited", ok=True),
        turn=0,
        tool_index=0,
        redelivered=True,
    )
    spec = spec_for_recovered_row(row)
    assert spec.call.name == "edit"


def test_false_stays_safe_replay_for_unsafe_tool():
    from types import SimpleNamespace

    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_recovery_result import spec_for_recovered_row
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    row = SimpleNamespace(
        call=ToolCall("edit", {"path": "a.py"}, "c1"),
        outcome=ToolOutcome(model_text="edited", ok=True),
        turn=0,
        tool_index=0,
        redelivered=False,
    )
    with pytest.raises(RecoveryFailed):
        spec_for_recovered_row(row)
