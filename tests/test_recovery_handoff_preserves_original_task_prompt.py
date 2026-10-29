"""The same committed result produces the same continuation after restart."""
import pytest

from codey.operations.task_loop import run_task_kernel
from tests.support.kernel_restart import paused_batch


@pytest.mark.parametrize("native", [False, True])
def test_recovery_keeps_original_goal_contract_results_and_request_sequence(tmp_path, native):
    traces = []
    for paused in (False, True):
        root = tmp_path / str(paused)
        root.mkdir()
        with paused_batch(root, native=native, pause=paused) as f:
            result = run_task_kernel(f.session(), request=f.request)
            assert result.completed, result
            assert len(f.executions) == 1
            assert "ORIGINAL_GOAL" in f.events[0][1]
            assert "ALLOWED_CONTRACT" in f.events[0][1]
            assert "ORIGINAL_RESULT" in str(f.events[1])
            traces.append(f.events)
    assert traces[0] == traces[1]
