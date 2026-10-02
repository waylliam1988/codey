"""Capture completion owns the drain deadline, independently of thread teardown."""

import io
import threading
import time
from types import SimpleNamespace

import pytest

from codey.runtime.core import cancellation


def test_eof_finishes_capture_without_waiting_for_thread_teardown(monkeypatch):
    release = threading.Event()
    target_finished = threading.Event()
    real_thread = threading.Thread
    readers = []

    class SlowTeardownThread(real_thread):
        def run(self):
            try:
                super().run()
            finally:
                target_finished.set()
                release.wait(5)

    def start_reader(*args, **kwargs):
        reader = SlowTeardownThread(*args, **kwargs)
        readers.append(reader)
        return reader

    monkeypatch.setattr(cancellation.threading, "Thread", start_reader)
    monkeypatch.setattr(cancellation, "DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "READER_CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "_terminate_process_tree", lambda *args: None)
    stream = io.BytesIO(b"actual output")
    proc = SimpleNamespace(stdout=stream, stderr=None, wait=lambda **kwargs: 0)
    try:
        result = cancellation.wait_process(proc, None, "test", 1, capture_limit_bytes=1024)
        assert result.stdout == "actual output"
        assert target_finished.is_set()
        assert stream.closed
    finally:
        release.set()
        for reader in readers:
            reader.join(5)


def test_blocked_read_deadline_does_not_depend_on_native_thread_join(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    readers = []
    real_thread = threading.Thread

    class Stream:
        closed = False

        def read(self, size):
            entered.set()
            release.wait(5)
            return b""

        def close(self):
            self.closed = True

    def start_reader(*args, **kwargs):
        reader = real_thread(*args, **kwargs)
        readers.append(reader)
        return reader

    monkeypatch.setattr(cancellation.threading, "Thread", start_reader)
    monkeypatch.setattr(real_thread, "join", lambda *args, **kwargs: pytest.fail("native thread join must not own pipe drain"))
    monkeypatch.setattr(cancellation, "DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "READER_CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cancellation, "_terminate_process_tree", lambda *args: None)
    stream = Stream()
    proc = SimpleNamespace(stdout=stream, stderr=None, wait=lambda **kwargs: 0)
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
