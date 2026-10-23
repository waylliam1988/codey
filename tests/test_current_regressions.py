"""Regression tests for cross-session UI admission and response latency."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import Mock

from codey.app import model_settings
from codey.app.context import AppContext


def _run_with_timeout(fn: Any, timeout: float = 0.25) -> tuple[threading.Thread, list[Any]]:
    result: list[Any] = []
    thread = threading.Thread(target=lambda: result.append(fn()), daemon=True)
    thread.start()
    thread.join(timeout)
    return thread, result


def test_model_settings_read_does_not_wait_for_long_context_lock(tmp_path) -> None:
    ctx = AppContext(tmp_path)
    ctx.lock.acquire()
    try:
        thread, result = _run_with_timeout(lambda: model_settings.settings_response(ctx))
        assert not thread.is_alive(), "Settings read waited for an unrelated task lifecycle lock"
        assert result and result[0][0] == 200
    finally:
        ctx.lock.release()
        ctx.close()


def test_two_approval_decisions_use_durable_wakeup_without_continuation_workers(tmp_path, monkeypatch) -> None:
    from tests.support.shell_approval import seed_requested_approval

    ctx = AppContext(tmp_path)
    seed_requested_approval(ctx, approval_id="first", command="echo first")
    seed_requested_approval(ctx, approval_id="second", command="echo second")
    submit = Mock(return_value=None)

    try:
        first_result: list[tuple[int, dict[str, Any]]] = []
        first = threading.Thread(
            target=lambda: first_result.append(ctx.shell_approval_workflow.accept("first", True, submit)),
            daemon=True,
        )
        first.start()
        first.join(2)
        assert not first.is_alive()
        second = ctx.shell_approval_workflow.accept("second", False, submit)
        assert second[0] == 202
        assert first_result and first_result[0][0] == 202
        submit.assert_not_called()
    finally:
        first.join(5)
        ctx.shell_approval_workflow.wait(5)
        ctx.close()

