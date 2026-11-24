import threading
import time

import pytest

from codey.codemode.runtime import CodeModeRequest, CodeModeSession


def test_session_can_retry_execution_when_starting_the_thread_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    session = CodeModeSession()
    request = CodeModeRequest("return 42;", (), lambda _name, _args: None, execution_id="test-startup")

    def fail_start(thread: threading.Thread) -> None:
        assert thread.name == "codemode-test-startup"
        raise RuntimeError("simulated thread startup failure")

    with monkeypatch.context() as startup:
        startup.setattr("codey.codemode.runtime.threading.Thread.start", fail_start)
        with pytest.raises(RuntimeError, match="thread startup failure"):
            session.start(request)

    handle = session.start(request)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = session.wait(handle, yield_ms=50)
        if any(event.kind in {"done", "error", "terminated"} for event in events):
            break
    outcome = session.outcome(handle)
    assert outcome.result is not None and outcome.result.ok and outcome.result.value == 42
