from __future__ import annotations

from codey.app.context import AppContext


def test_shell_result_publishes_bounded_failure_facts(tmp_path):
    ctx = AppContext(tmp_path)
    sub = ctx.event_bus.subscribe()
    try:
        ctx.record_shell_result({
            "type": "shell_result",
            "id": "approval",
            "session_id": "session",
            "run_id": "run",
            "status": "output_read_error",
            "execution_id": "execution-1",
            "output": "partial",
        })
        event = sub.get(timeout=1)
        assert event["failure"]["code"] == "output.unavailable"
        assert event["failure"]["effect"] == "unknown"
        assert event["failure"]["execution_id"] == "execution-1"
    finally:
        ctx.close()
