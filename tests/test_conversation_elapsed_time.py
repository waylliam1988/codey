from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.app import conversation_runtime
from codey.app.context import AppContext
from codey.runtime.core.api_selection import ApiRunSelection


class _Clock:
    def __init__(self, ticks: Iterator[float]) -> None:
        self.ticks = ticks

    def monotonic(self) -> float:
        return next(self.ticks)


def test_elapsed_time_comes_from_the_server_run_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = AppContext(tmp_path)
    seen: list[dict[str, object]] = []

    def on_event(payload: dict[str, object]) -> None:
        seen.append(dict(payload))

    monkeypatch.setattr(ctx, "_on_event_emitted", on_event)
    ticks = iter([100.0, 142.75])
    try:
        with monkeypatch.context() as patch:
            # Only this runtime consumes the two event timestamps. Scheduler
            # deadlines must keep their real clock while AppContext is alive.
            patch.setattr(conversation_runtime, "time", _Clock(ticks))
            ctx.emit({"type": "task_start", "run_id": "r", "session_id": "s", "mode": "chat"})
            ctx.emit({"type": "task_done", "run_id": "r", "session_id": "s", "stop_reason": "done"})
        assert seen[-1]["elapsed_ms"] == 42_750
    finally:
        ctx.close()


def test_close_retains_queue_resources_until_its_live_run_terminalizes(tmp_path: Path) -> None:
    ctx = AppContext(tmp_path)
    request: dict[str, object] = {"session_id": "s", "run_id": "r", "task": "read", "provider": "local"}
    ctx.input_queue.create("input", request, model_selection=ApiRunSelection("local", "fixture", "fixture", "openai-completions", True))

    # Directly placeable input is admitted as submitted; the active run slot
    # is the shutdown resource that must remain alive until its terminal event.
    ctx.reserve_run(session_id="s", project=None, task="read", provider_id="local", run_id="r")
    assert ctx.close() is False
    item = ctx.input_queue.get("input")
    assert item is not None and item["state"] == "submitted"
    ctx.emit({"type": "task_done", "run_id": "r", "session_id": "s", "stop_reason": "stopped"})
    ctx.release_run("r")
    assert ctx.close() is True
