"""Formatting failures retain original receipts and never resend or execute."""
import pytest

from codey.operations.task_loop import run_task_kernel
from tests.support.kernel_restart import paused_batch


@pytest.mark.parametrize("native", [False, True])
def test_recovery_format_error_preserves_receipt_without_new_effects(tmp_path, monkeypatch, native):
    with paused_batch(tmp_path, native=native) as f:
        original = f.durable.effects("r")
        sends = list(f.events)
        def unavailable(*_args, **_kwargs):
            raise ValueError("format boom")
        monkeypatch.setattr("codey.operations.kernel_transport._native_tool_results" if native
                            else "codey.operations.kernel_prompt._format_results", unavailable)
        result = run_task_kernel(f.session(), request=f.request)
        assert not result.completed
        assert result.stop_reason in {"recovery_failure", "controller_failure"}
        assert "format boom" in result.summary
        assert f.events == sends and len(f.executions) == 1
        assert [e for e in f.durable.effects("r") if e.category == "tool_call"] == [e for e in original if e.category == "tool_call"]
