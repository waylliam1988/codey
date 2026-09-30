"""Unsafe-tool replay classification has one owner in tool definitions.

Both recovery orchestration and recovery-result construction must consult
``codey.toolchain.tool_spec.tool_requires_trusted_recovery``. No recovery
module owns the rule, and the rule stays fail-closed: unknown tools,
non-safe replay classes, and registry errors all require trusted recovery.
Frozen-snapshot callers keep passing the frozen spec explicitly.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_trust_query_lives_in_tool_spec() -> None:
    from codey.toolchain import tool_spec

    assert callable(getattr(tool_spec, "tool_requires_trusted_recovery", None))
    assert "tool_requires_trusted_recovery" in getattr(tool_spec, "__all__", ())


def test_trust_query_is_fail_closed() -> None:
    from codey.toolchain.tool_spec import tool_requires_trusted_recovery

    assert tool_requires_trusted_recovery("edit") is True
    assert tool_requires_trusted_recovery("run") is True
    assert tool_requires_trusted_recovery("shell") is True
    assert tool_requires_trusted_recovery("no_such_tool_xyz") is True
    assert tool_requires_trusted_recovery("read_file") is False
    assert tool_requires_trusted_recovery("list_dir") is False
    assert tool_requires_trusted_recovery("web_search") is False
    assert tool_requires_trusted_recovery("") is True


def test_recovery_modules_share_single_trust_owner() -> None:
    recovery_text = (ROOT / "codey/operations/kernel_recovery.py").read_text(encoding="utf-8-sig")
    result_text = (ROOT / "codey/operations/kernel_recovery_result.py").read_text(encoding="utf-8-sig")
    assert "tool_requires_trusted_recovery" in recovery_text
    assert "tool_requires_trusted_recovery" in result_text
    assert "_is_unsafe_tool" not in result_text


def test_frame_recovery_still_rejects_unsafe_replay() -> None:
    from types import SimpleNamespace

    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_recovery_result import spec_for_recovered_row
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    row = SimpleNamespace(
        call=ToolCall("edit", {"path": "a.txt"}, "c1"),
        outcome=ToolOutcome(model_text="x", ok=True),
        turn=0,
        tool_index=0,
        redelivered=False,
    )
    try:
        spec_for_recovered_row(row)
    except RecoveryFailed:
        return
    raise AssertionError("unsafe frame replay must raise RecoveryFailed")
