"""Capture completion owns the drain deadline, independently of thread teardown."""

import io
import threading
import time
from collections.abc import Callable
from threading import Thread as BaseThread

import pytest

from codey.runtime.core import cancellation
from codey.runtime.core.process_io import ChildProcess, ReadPipe


class ExitedProcess:
    pid = 0
    args = "test"
    stderr: ReadPipe | None = None

    def __init__(self, stream: ReadPipe) -> None:
        self.stdout = stream

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return 0

    def kill(self) -> None:
        raise AssertionError("test process must not be killed")

    def terminate(self) -> None:
        raise AssertionError("test process must not be terminated")


def ignore_tree_cleanup(proc: ChildProcess, job: cancellation.ProcessTreeOwner | None) -> None:
    pass


def test_eof_finishes_capture_without_waiting_for_thread_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    target_finished = threading.Event()
    readers: list[threading.Thread] = []

    class SlowTeardownThread(BaseThread):
        def __init__(self, *, target: Callable[[ReadPipe, cancellation._StreamPump], None],
                     args: tuple[ReadPipe, cancellation._StreamPump], daemon: bool) -> None:
            super().__init__(target=target, args=args, daemon=daemon)

        def run(self) -> None:
            try:
                super().run()
            finally:
                target_finished.set()
                release.wait(5)

    def start_reader(*, target: Callable[[ReadPipe, cancellation._StreamPump], None],
                     args: tuple[ReadPipe, cancellation._StreamPump], daemon: bool) -> threading.Thread:
        reader = SlowTeardownThread(target=target, args=args, daemon=daemon)
        readers.append(reader)
        return reader

    monkeypatch.setattr(threading, "Thread", start_reader)
    monkeypatch.setattr(cancellation, "DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "READER_CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "_terminate_process_tree", ignore_tree_cleanup)
    stream = io.BytesIO(b"actual output")
    proc = ExitedProcess(stream)
    try:
        result = cancellation.wait_process(proc, None, "test", 1, capture_limit_bytes=1024)
        assert result.stdout == "actual output"
        assert target_finished.is_set()
        assert stream.closed
    finally:
        release.set()
        for reader in readers:
            reader.join(5)


def test_blocked_read_deadline_does_not_depend_on_native_thread_join(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    entered = threading.Event()
    readers: list[threading.Thread] = []

    class Stream:
        closed = False

        def read(self, size: int = -1, /) -> bytes:
            entered.set()
            release.wait(5)
            return b""

        def close(self) -> None:
            self.closed = True

    def start_reader(*, target: Callable[[ReadPipe, cancellation._StreamPump], None],
                     args: tuple[ReadPipe, cancellation._StreamPump], daemon: bool) -> threading.Thread:
        reader = BaseThread(target=target, args=args, daemon=daemon)
        readers.append(reader)
        return reader

    monkeypatch.setattr(threading, "Thread", start_reader)
    def unavailable_join(self: threading.Thread, timeout: float | None = None) -> None:
        pytest.fail("native thread join must not own pipe drain")

    monkeypatch.setattr("tests.test_process_capture_completion_ownership.BaseThread.join", unavailable_join)
    monkeypatch.setattr(cancellation, "DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "READER_CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "_terminate_process_tree", ignore_tree_cleanup)
    stream = Stream()
    proc = ExitedProcess(stream)
    started = time.monotonic()
    try:
        with pytest.raises(cancellation.PipeDrainTimeout):
            cancellation.wait_process(proc, None, "test", 1, capture_limit_bytes=1024)
        assert entered.is_set()
        assert time.monotonic() - started < 2
        assert stream.closed is False
    finally:
        release.set()
        # Join after restoring the deliberately unavailable native join.
        monkeypatch.undo()
        for reader in readers:
            reader.join(5)
