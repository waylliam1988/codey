import threading

import pytest

from codey.automation import browser_worker
from codey.runtime.core import cancellation


def test_running_call_reports_close_when_cleanup_finishes_before_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_worker, "_POLL_INTERVAL", 30.0)
    worker = browser_worker.BrowserWorker(name="test-browser-close-outcome")
    started = threading.Event()
    results: list[str] = []
    errors: list[Exception] = []
    cleanup_threads: list[int] = []

    def run() -> str:
        event = cancellation.current_event()
        assert event is not None
        started.set()
        assert event.wait(10)
        return "late result"

    def call() -> None:
        try:
            results.append(worker.call(run, on_abandoned=cleanup))
        except Exception as exc:
            errors.append(exc)

    def cleanup() -> None:
        cleanup_threads.append(threading.get_ident())

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    try:
        assert started.wait(5)
    finally:
        closed = worker.close()
        caller.join(5)

    assert closed
    assert not caller.is_alive()
    assert not results
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "browser worker is closed"
    assert cleanup_threads == [worker._thread_id]
