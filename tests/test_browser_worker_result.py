import pytest

from codey.automation.browser_worker import BrowserWorker


def test_worker_returns_an_exception_object_when_it_is_the_result() -> None:
    worker = BrowserWorker(name="test-browser-value")
    value = RuntimeError("worker value")
    try:
        assert worker.call(lambda: value, timeout=2) is value
    finally:
        worker.close()


def test_worker_reraises_a_raised_exception() -> None:
    worker = BrowserWorker(name="test-browser-error")
    value = RuntimeError("worker failure")

    def fail() -> None:
        raise value

    try:
        with pytest.raises(RuntimeError) as exc:
            worker.call(fail, timeout=2)
        assert exc.value is value
        assert worker.call(lambda: 3, timeout=2) == 3
    finally:
        worker.close()
