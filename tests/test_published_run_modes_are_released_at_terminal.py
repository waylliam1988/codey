"""Terminal publication must release mode metadata even when mode is explicit."""

from codey.app.context import AppContext


def test_terminal_with_explicit_mode_does_not_retain_finished_run(tmp_path):
    context = AppContext(tmp_path)
    try:
        context.emit({"type": "task_start", "run_id": "r", "mode": "project"})
        context.emit({"type": "task_done", "run_id": "r", "mode": "project"})
        assert not context._run_modes
    finally:
        context.close()
