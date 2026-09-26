"""Worker local gaps: structured write failure + idempotent re-close."""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.providers.diagnostics import ProviderActionError
from codey.providers.worker import WorkerChatProvider, _PendingRequest, _WorkerSession


def _provider() -> WorkerChatProvider:
    provider = WorkerChatProvider.__new__(WorkerChatProvider)
    provider.provider_id = "qwen"
    provider.override = SimpleNamespace(root=Path("."), generation=1)
    provider.port = 19223
    provider.state_home = Path(".")
    provider.name = "qwen worker"
    provider.last_failure = None
    provider._life_lock = threading.Lock()
    provider._request_lock = threading.Lock()
    provider._session = None
    return provider


def _deadline(seconds: float = 5.0) -> float:
    return time.monotonic() + seconds


class WorkerLocalFixTests(unittest.TestCase):
    def test_await_write_surfaces_structured_failure(self) -> None:
        provider = _provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = _WorkerSession(proc=proc, job=None)
        provider._session = session
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
            session.write_error = "provider worker stdin is unavailable: broken"
            session.write_done.set()
        with self.assertRaises(ProviderActionError) as raised:
            provider._await_write(session, pending, _deadline())
        self.assertIn("stdin is unavailable", raised.exception.failure.message)
        with session.lock:
            self.assertIsNone(session.pending)

    def test_retire_recloses_pipes_skipped_while_thread_alive(self) -> None:
        provider = _provider()
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.stdin = mock.Mock()
        proc.stdout = mock.Mock()
        proc.stderr = mock.Mock()
        session = _WorkerSession(proc=proc, job=None)
        provider._session = session
        # First retire sees a live reader and skips its pipe.
        live_reader = mock.Mock()
        live_reader.is_alive.return_value = True
        session.reader = live_reader
        session.stderr_reader = None
        session.writer = None
        first = provider._retire_session_locked(session)
        self.assertFalse(first)
        proc.stdout.close.assert_not_called()
        # Thread ends; a second close must finish the skipped pipe.
        live_reader.is_alive.return_value = False
        provider._retire_session_locked(session)
        proc.stdout.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
