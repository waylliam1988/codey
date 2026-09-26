"""Regression tests for the cold-start hardening batch.

Covers: task_runtime settle propagation + turn-budget clamp, operation-state
load fail-closed, EventBus overflow accounting + SSE resync cursor, provider
worker self-heal, provider probe_error signal, conversation prune robustness,
research result-id normalization, headless shell-approval expiry, and the
POST body-read timeout.
"""

from __future__ import annotations

import contextlib
import io
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.app import api as app_api
from codey.app.context import AppContext
from codey.app.event_bus import EventBus, EventSubscriber, SsePayload
from codey.app.headless_runner import HeadlessAppContext
from codey.providers import DEFAULT_PROVIDER_ID, PROVIDER_LABELS
from codey.providers.worker import (
    WORKER_LINE_MAX_CHARS,
    WORKER_STDERR_CHUNK_CHARS,
    WORKER_STDERR_TAIL_CHUNKS,
    WorkerChatProvider,
    _PendingRequest,
    _WorkerSession,
)
from codey.research.controller import ResearchController
from codey.runtime.core.operation_state import (
    RuntimeOperationStore,
    RuntimeOperationTransitionError,
)
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.runtime.write.task_runtime import TaskRuntime, _turn_budget
from codey.storage.conversation_store import ConversationStore
from codey.task.model import TaskSubmission


def _submission(**overrides: object) -> TaskSubmission:
    values: dict[str, object] = {
        "session_id": "s1",
        "project": None,
        "task": "do it",
        "max_turns": 3,
        "continue_task": False,
        "provider_id": "qwen",
        "run_id": "run-1",
    }
    values.update(overrides)
    return TaskSubmission(**values)  # type: ignore[arg-type]


class TurnBudgetTests(unittest.TestCase):
    def test_turn_budget_clamps_to_min_one(self) -> None:
        self.assertEqual(_turn_budget(_submission(max_turns=0)), 1)
        self.assertEqual(_turn_budget(_submission(max_turns=-5)), 1)
        self.assertEqual(_turn_budget(_submission(max_turns=7)), 7)

    def test_turn_budget_falls_back_to_one_on_garbage(self) -> None:
        self.assertEqual(_turn_budget(_submission(max_turns="abc")), 1)


class SettlePropagationTests(unittest.TestCase):
    def test_settle_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(RuntimeSessionLog(Path(td)), executor=lambda req: None)
            with mock.patch.object(
                runtime.mutations,
                "mark_terminal",
                side_effect=RuntimeOperationTransitionError("boom"),
            ), self.assertRaises(RuntimeOperationTransitionError):
                runtime._settle_if_open(_submission(), SimpleNamespace(summary="", status="completed", reason=""))

    def test_settle_is_noop_when_inner_flow_already_settled(self) -> None:
        from codey.runtime.core.outcome import OperationOutcome

        with tempfile.TemporaryDirectory() as td:
            runtime = TaskRuntime(RuntimeSessionLog(Path(td)), executor=lambda req: None)
            request = _submission()
            runtime.mutations.accept_operation(
                session_id=request.session_id,
                run_id=request.run_id,
                project="",
                provider_id=request.provider_id,
                turn_budget=3,
                max_repair_rounds=1,
                task_kind="task",
            )
            runtime.mutations.mark_terminal(
                request.session_id,
                request.run_id,
                stop_reason="done",
                summary_chars=4,
                turns=1,
                max_turns=3,
                provider=request.provider_id,
            )
            # Must not raise even though turns/max_turns differ from the backstop's.
            runtime._settle_if_open(request, OperationOutcome.completed(summary="done!"))


class OperationStoreLoadTests(unittest.TestCase):
    def test_load_returns_none_when_log_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeOperationStore(RuntimeSessionLog(Path(td)))
            self.assertIsNone(store.load("nope", "run-9"))

    def test_load_returns_state_for_healthy_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            RuntimeMutationLine(log).accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="qwen",
                turn_budget=3,
                max_repair_rounds=1,
                task_kind="project",
            )
            state = RuntimeOperationStore(log).load("s1", "run-1")
        self.assertIsNotNone(state)

    def test_load_raises_instead_of_returning_none_on_corrupt_tail(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            RuntimeMutationLine(log).accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="qwen",
                turn_budget=3,
                max_repair_rounds=1,
                task_kind="project",
            )
            # Simulate on-disk corruption from a stale writer: a well-formed
            # envelope carrying an operation_state payload with an illegal leaf.
            envelope = next(
                entry.to_payload()
                for entry in log.entries("s1")
                if entry.kind == "operation_state"
            )
            envelope["entry_id"] = "entry-corrupt-tail"
            envelope["batch_id"] = "batch-corrupt-tail"
            envelope["batch_index"] = 0
            envelope["batch_count"] = 1
            envelope["payload"] = {
                **envelope["payload"],
                "leaf": "nope-not-a-leaf",
            }
            path = log.path_for("s1")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            with self.assertRaises(RuntimeOperationTransitionError):
                RuntimeOperationStore(log).load("s1", "run-1")


class EventBusOverflowTests(unittest.TestCase):
    def test_overflow_keeps_a_resync_marker(self) -> None:
        bus = EventBus(replay_limit=16)
        sub = bus.subscribe(maxsize=2)
        for index in range(5):
            bus.emit({"type": "turn", "turn": index})
        rows = []
        while True:
            try:
                rows.append(sub.get_nowait())
            except queue.Empty:
                break
        markers = [row for row in rows if row.get("type") == "resync_required"]
        self.assertTrue(markers)
        self.assertGreaterEqual(markers[-1]["dropped"], 1)

    def test_double_full_retains_the_drop_count(self) -> None:
        sub = EventSubscriber(maxsize=2)
        payload = SsePayload({"type": "turn", "turn": 1}, event_id=1)
        with mock.patch.object(sub, "put_nowait", side_effect=queue.Full):
            EventBus._put_for_subscriber(sub, payload, {"type": "turn"})
        self.assertEqual(sub.dropped, 1)

    def test_expired_replay_is_marker_only_and_never_repeats(self) -> None:
        bus = EventBus(replay_limit=4)
        for index in range(6):
            bus.emit({"type": "turn", "turn": index})
        rows = bus.replay_events_after(1)
        self.assertEqual(len(rows), 1)
        marker_id, marker = rows[0]
        self.assertEqual(marker["type"], "resync_required")
        self.assertGreater(marker_id, 1)
        # Adopting the marker strictly advances the cursor: the off-by-one
        # case (cursor == oldest_retained - 1) must not resync twice.
        self.assertEqual(bus.replay_events_after(marker_id), [])
        # Any other expired cursor converges on the same marker id.
        self.assertEqual(bus.replay_events_after(2)[0][0], marker_id)


class _RecordedStringIO(io.StringIO):
    """StringIO honoring readline(size), recording every requested size."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.sizes: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.sizes.append(size)
        return super().readline(size)


class _GatedStderr:
    """readline-only fake serving scripted chunks, then blocking on a gate."""

    def __init__(self, chunks: list[str], release: threading.Event) -> None:
        self._chunks = list(chunks)
        self._release = release
        self.sizes: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.sizes.append(size)
        if self._chunks:
            chunk = self._chunks.pop(0)
            return chunk[:size] if size is not None and size >= 0 else chunk
        assert self._release.wait(timeout=10.0)
        return ""


class WorkerSelfHealTests(unittest.TestCase):
    @staticmethod
    def _mark_process_exited(proc: object, _job: object) -> None:
        # A mocked tree terminator must also model the child's exit. Merely
        # recording the call leaves poll() reporting a live process forever.
        proc.poll.return_value = 0

    def _provider(self) -> WorkerChatProvider:
        provider = WorkerChatProvider.__new__(WorkerChatProvider)
        provider.provider_id = "qwen"
        provider.override = SimpleNamespace(root=Path("."), generation=1)
        provider.port = 19222
        provider.state_home = Path(".")
        provider.name = "qwen worker"
        provider.last_failure = None
        provider._life_lock = threading.Lock()
        provider._request_lock = threading.Lock()
        provider._session = None
        return provider

    def _session_for(
        self,
        provider: WorkerChatProvider,
        proc: object | None = None,
        *,
        job: object | None = None,
    ) -> _WorkerSession:
        if proc is None:
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.stdin = mock.Mock()
        session = _WorkerSession(proc=proc, job=job)  # type: ignore[arg-type]
        provider._session = session
        self._spawn_writer(provider, session)
        return session

    def _spawn_writer(
        self, provider: WorkerChatProvider, session: _WorkerSession
    ) -> threading.Thread:
        """Run the session's stdin owner, as _start_session_locked does."""
        writer = threading.Thread(
            target=provider._writer_loop, args=(session,), daemon=True,
        )
        session.writer = writer
        writer.start()
        self.addCleanup(self._cleanup_test_writer, session, writer)
        return writer

    def _cleanup_test_writer(
        self, session: _WorkerSession, writer: threading.Thread
    ) -> None:
        with session.lock:
            session.closed = True
            session.write_event.set()
        writer.join(timeout=5.0)
        if writer.is_alive():
            self.fail(f"writer thread leaked: {writer.name}")

    @staticmethod
    def _deadline(seconds: float = 5.0) -> float:
        return time.monotonic() + seconds

    def test_ensure_running_restarts_dead_worker(self) -> None:
        provider = self._provider()
        dead = mock.Mock()
        dead.poll.return_value = 1
        dead.stdin = mock.Mock()
        self._session_for(provider, dead)
        fresh = mock.Mock()
        fresh.poll.return_value = None
        fresh.stdin = mock.Mock()

        def fake_start(inner_self) -> None:
            inner_self._session = _WorkerSession(proc=fresh, job=None)

        with (
            mock.patch.object(WorkerChatProvider, "_start_session_locked", fake_start),
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ) as terminate,
            provider._life_lock,
        ):
            running = provider._ensure_live_session_locked()
        self.assertIs(running.proc, fresh)
        terminate.assert_called_once_with(dead, None)
        self.assertIs(provider._session.proc, fresh)

    def test_single_flight_registers_pending_before_write(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        seen: list[bool] = []

        def fake_write(data: str) -> None:
            with session.lock:
                seen.append(session.pending is not None)
            raise OSError("stop after proving registration")

        proc.stdin.write.side_effect = fake_write
        with self.assertRaises(RuntimeError):
            provider._request_locked("send", {"text": "hi"}, self._deadline())
        self.assertEqual(seen, [True])

    def test_line_exactly_at_cap_plus_newline_parses(self) -> None:
        # A line of exactly MAX chars plus its newline fits in one
        # readline(MAX+1): it is a valid frame, not an over-limit one.
        provider = self._provider()
        prefix = '{"id":"a","ok":true,"result":"'
        suffix = '"}\n'
        line = prefix + "p" * (WORKER_LINE_MAX_CHARS - len(prefix) - len(suffix) + 1) + suffix
        self.assertEqual(len(line), WORKER_LINE_MAX_CHARS + 1)
        proc = mock.Mock()
        proc.stdout = _RecordedStringIO(line)
        # Exited after the single frame: trailing EOF is a normal exit,
        # not a live-framing failure.
        proc.poll.return_value = 1
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="a", method="send")
        with session.lock:
            session.pending = pending
        provider._read_loop(session)
        self.assertEqual(session.terminal_error, "")
        self.assertIsNotNone(pending.response)
        assert pending.response is not None
        self.assertEqual(pending.response["id"], "a")
        self.assertTrue(proc.stdout.sizes)
        self.assertLessEqual(max(proc.stdout.sizes), WORKER_LINE_MAX_CHARS + 1)

    def test_overlong_line_condemns_session_and_spares_next_frame(self) -> None:
        provider = self._provider()
        huge = "y" * (WORKER_LINE_MAX_CHARS + 100)
        reply = '{"id":"late","ok":true,"result":"kept"}\n'
        proc = mock.Mock()
        proc.stdout = _RecordedStringIO(huge + "\n" + reply)
        proc.poll.return_value = None
        session = self._session_for(provider, proc)
        provider._read_loop(session)
        # No single read ever buffered the whole line, and the reader
        # stopped at the condemned frame instead of draining the reply.
        self.assertTrue(proc.stdout.sizes)
        self.assertLessEqual(max(proc.stdout.sizes), WORKER_LINE_MAX_CHARS + 1)
        self.assertIn("exceeded", session.terminal_error)
        self.assertIsNone(session.pending)
        self.assertIn('"late"', proc.stdout.read())

    def test_malformed_frame_condemns_session(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.stdout = _RecordedStringIO("not-json\n")
        proc.poll.return_value = None
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        provider._read_loop(session)
        self.assertIn("malformed", session.terminal_error)
        self.assertTrue(pending.done.is_set())

    def test_wrong_id_is_protocol_error_not_silent_skip(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        # A flood of foreign ids must not be consumed forever: the first
        # mismatch condemns the session and the waiter fails promptly.
        provider._deliver_response(session, {"id": "foreign", "ok": True})
        self.assertIn("unknown request", session.terminal_error)
        with (
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._wait_for_response(session, pending, self._deadline())
        self.assertIn("unknown request", raised.exception.failure.message)
        self.assertIsNone(provider._session)

    def test_condemned_session_fails_waiter_and_is_not_reused(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        session = self._session_for(provider, proc)
        with session.lock:
            session.terminal_error = "provider worker output exceeded 1 chars"
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        with (
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ) as terminate,
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._wait_for_response(session, pending, self._deadline())
        self.assertIn("exceeded", raised.exception.failure.message)
        terminate.assert_called_once_with(proc, None)
        self.assertIsNone(provider._session)

    def test_overlong_before_any_request_restarts_transparently(self) -> None:
        provider = self._provider()
        condemned = mock.Mock()
        condemned.poll.return_value = None
        condemned.stdin = mock.Mock()
        condemned.stdout = _RecordedStringIO("y" * (WORKER_LINE_MAX_CHARS + 10))
        session = self._session_for(provider, condemned)
        # The reader sees the over-limit frame before any request exists.
        provider._read_loop(session)
        self.assertNotEqual(session.terminal_error, "")

        fresh = mock.Mock()
        fresh.poll.return_value = None
        fresh.stdin = mock.Mock()
        written = threading.Event()
        wires: list[str] = []

        def fake_write(data: str) -> None:
            wires.append(data)
            written.set()

        fresh.stdin.write.side_effect = fake_write
        out: dict[str, object] = {}

        def fake_start(inner_self) -> None:
            fresh_session = _WorkerSession(proc=fresh, job=None)
            inner_self._session = fresh_session
            self._spawn_writer(inner_self, fresh_session)

        def do_request() -> None:
            out["result"] = provider._request_locked(
                "send", {"text": "hi"}, self._deadline(),
            )

        with mock.patch.object(
            WorkerChatProvider, "_start_session_locked", fake_start,
        ), mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
        ) as terminate:
            worker = threading.Thread(target=do_request)
            worker.start()
            self.assertTrue(written.wait(timeout=10.0))
            with provider._session.lock:
                pending = provider._session.pending
                assert pending is not None
                request_id = pending.request_id
            provider._deliver_response(
                provider._session,
                {"id": request_id, "ok": True, "result": "ok"},
            )
            worker.join(timeout=10.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(out["result"], "ok")
        terminate.assert_called_once_with(condemned, None)
        self.assertEqual(len(wires), 1)

    def test_stop_is_checked_each_loop_and_retires_session(self) -> None:
        from codey.providers.diagnostics import ProviderActionError  # noqa: F401
        from codey.runtime.core import cancellation as cancel

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        calls = {"count": 0}
        real_check = cancel.check

        def counting_check() -> None:
            calls["count"] += 1
            if calls["count"] >= 2:
                raise cancel.TaskCancelled("task stopped")
            real_check()

        with (
            mock.patch.object(cancel, "check", side_effect=counting_check),
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
            self.assertRaises(cancel.TaskCancelled),
        ):
            provider._wait_for_response(session, pending, self._deadline(30.0))
        # Checked before waiting and again after the short wake: Stop can
        # never be starved by an unrelated reply flood.
        self.assertGreaterEqual(calls["count"], 2)
        self.assertIsNone(provider._session)
        with session.lock:
            self.assertIsNone(session.pending)

    def test_thread_start_failure_cleans_process(self) -> None:
        provider = self._provider()
        real_proc = mock.Mock()
        real_proc.stdin = mock.Mock()
        real_proc.stdout = mock.Mock()
        real_proc.stderr = mock.Mock()
        real_proc.poll.return_value = None
        job = mock.Mock()
        with (
            mock.patch(
                "codey.providers.worker.subprocess.Popen", return_value=real_proc,
            ),
            mock.patch(
                "codey.providers.worker.cancellation.attach_process_tree",
                return_value=job,
            ) as attach,
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ) as terminate,
            mock.patch.object(
                threading.Thread, "start", side_effect=RuntimeError("no threads"),
            ),
            provider._life_lock,
            self.assertRaises(RuntimeError),
        ):
            provider._start_session_locked()
        attach.assert_called_once_with(real_proc)
        terminate.assert_called_once_with(real_proc, job)
        job.close.assert_called_once()
        for stream in (real_proc.stdin, real_proc.stdout, real_proc.stderr):
            stream.close.assert_called()
        self.assertIsNone(provider._session)

    def test_blocked_stdin_write_does_not_block_close(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        self._session_for(provider, proc)
        release = threading.Event()
        entered = threading.Event()

        def gated_flush() -> None:
            entered.set()
            assert release.wait(timeout=10.0)
            raise OSError("broken pipe")

        proc.stdin.flush.side_effect = gated_flush
        errors: list[BaseException] = []

        def do_request() -> None:
            try:
                provider._request_locked("send", {"text": "hi"}, self._deadline())
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
        ) as terminate:
            worker = threading.Thread(target=do_request)
            worker.start()
            # Proven inside flush(), not merely "probably there".
            self.assertTrue(entered.wait(timeout=10.0))
            closer = threading.Thread(target=provider.close)
            closer.start()
            closer.join(timeout=10.0)
            self.assertFalse(closer.is_alive())
            release.set()
            worker.join(timeout=10.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        # close() won the race and retired the generation: the request ends
        # with the generation gone, and the late write failure must not
        # terminate anything again.
        self.assertIsInstance(errors[0], ProviderActionError)
        self.assertIn("exited", errors[0].failure.message)
        terminate.assert_called_once_with(proc, None)

    def test_write_failure_after_replacement_spares_new_session(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        old = mock.Mock()
        old.poll.return_value = None
        old.stdin = mock.Mock()
        self._session_for(provider, old)
        release = threading.Event()
        entered = threading.Event()

        def gated_flush() -> None:
            entered.set()
            assert release.wait(timeout=10.0)
            raise OSError("broken pipe")

        old.stdin.flush.side_effect = gated_flush
        errors: list[BaseException] = []

        def do_request() -> None:
            try:
                provider._request_locked("send", {"text": "hi"}, self._deadline())
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
        ) as terminate:
            worker = threading.Thread(target=do_request)
            worker.start()
            self.assertTrue(entered.wait(timeout=10.0))
            # A concurrent close wins the race: the late write failure
            # must not reap the replacement. Retire marks closed, as in
            # production; the waiter observes closed, never a pointer.
            fresh = mock.Mock()
            fresh.poll.return_value = None
            fresh.stdin = mock.Mock()
            with provider._life_lock:
                old_session = provider._session
                assert old_session is not None
                with old_session.lock:
                    old_session.closed = True
                    if old_session.pending is not None:
                        old_session.pending.done.set()
                    old_session.write_event.set()
                provider._session = _WorkerSession(proc=fresh, job=None)
            release.set()
            worker.join(timeout=10.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ProviderActionError)
        terminate.assert_not_called()
        assert provider._session is not None
        self.assertIs(provider._session.proc, fresh)

    def test_blocked_pipe_stop_only_retires_without_close(self) -> None:
        """Stop alone unblocks a full pipe: no close() involved."""
        from codey.runtime.core import cancellation as cancel

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        self._session_for(provider, proc)
        release = threading.Event()
        entered = threading.Event()

        def gated_flush() -> None:
            entered.set()
            assert release.wait(timeout=10.0)
            raise OSError("broken pipe")

        proc.stdin.flush.side_effect = gated_flush
        errors: list[BaseException] = []
        stop = threading.Event()

        def do_request() -> None:
            try:
                with cancel.scope(stop):
                    provider._request("send", {"text": "hi"}, 30.0)
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
        ) as terminate:
            worker = threading.Thread(target=do_request)
            worker.start()
            # Proven blocked inside flush() before Stop lands.
            self.assertTrue(entered.wait(timeout=10.0))
            stop.set()
            worker.join(timeout=10.0)
            release.set()

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], cancel.TaskCancelled)
        # Stop retired only its own generation by killing the child,
        # which is what unblocks the writer.
        terminate.assert_called_once_with(proc, None)
        self.assertIsNotNone(provider._session)
        assert provider._session is not None
        self.assertTrue(provider._session.closed)
        assert provider._session.writer is not None
        provider._session.writer.join(timeout=10.0)
        self.assertTrue(provider.close())
        self.assertIsNone(provider._session)

    def test_stderr_tail_is_bounded_by_chunks_not_lines(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.stderr = _RecordedStringIO("e" * 1_000_000)
        session = self._session_for(provider, proc)
        provider._stderr_loop(session)
        self.assertTrue(proc.stderr.sizes)
        self.assertLessEqual(max(proc.stderr.sizes), WORKER_STDERR_CHUNK_CHARS)
        total = sum(len(chunk) for chunk in session.stderr_tail)
        self.assertLessEqual(
            total, WORKER_STDERR_TAIL_CHUNKS * WORKER_STDERR_CHUNK_CHARS,
        )
        suffix = provider._worker_error_suffix(session)
        self.assertLessEqual(len(suffix), 420)

    def test_stderr_small_line_arrives_while_child_alive(self) -> None:
        provider = self._provider()
        release = threading.Event()
        proc = mock.Mock()
        proc.stderr = _GatedStderr(["hello\n"], release)
        session = self._session_for(provider, proc)
        worker = threading.Thread(
            target=provider._stderr_loop, args=(session,),
            daemon=True,
        )
        worker.start()
        try:
            # Condition-based, not sleep-based: the short diagnostic must
            # land without waiting for a full chunk or EOF.
            deadline = time.monotonic() + 10.0
            while (
                "hello" not in " | ".join(session.stderr_tail)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(worker.is_alive())
            self.assertIn("hello", " | ".join(session.stderr_tail))
            self.assertTrue(proc.stderr.sizes)
            self.assertLessEqual(max(proc.stderr.sizes), WORKER_STDERR_CHUNK_CHARS)
        finally:
            release.set()
            worker.join(timeout=10.0)
        self.assertFalse(worker.is_alive())

    def test_stdout_read_error_condemns_current_generation(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None

        class _BrokenStdout:
            def readline(self, size: int = -1) -> str:
                raise OSError("stream broken")

        proc.stdout = _BrokenStdout()
        session = self._session_for(provider, proc)
        provider._read_loop(session)
        self.assertIn("unreadable", session.terminal_error)

    def test_stdout_eof_condemns_live_proc_but_not_exited_one(self) -> None:
        provider = self._provider()
        live = mock.Mock()
        live.poll.return_value = None
        live.stdout = _RecordedStringIO("")
        live_session = self._session_for(provider, live)
        provider._read_loop(live_session)
        self.assertIn("closed unexpectedly", live_session.terminal_error)

        exited = mock.Mock()
        exited.poll.return_value = 1
        exited.stdout = _RecordedStringIO("")
        exited_session = self._session_for(provider, exited)
        provider._read_loop(exited_session)
        self.assertEqual(exited_session.terminal_error, "")

    def test_stale_reader_cannot_pollute_new_generation(self) -> None:
        provider = self._provider()
        old_proc = mock.Mock()
        old_proc.poll.return_value = None
        old_session = self._session_for(provider, old_proc)
        old_tail = old_session.stderr_tail
        fresh_proc = mock.Mock()
        fresh_proc.poll.return_value = None
        fresh_session = _WorkerSession(proc=fresh_proc, job=None)
        with provider._life_lock:
            provider._session = fresh_session
        # Late verdicts, page events, and replies on the detached object
        # change nothing of the new generation: ownership is by object,
        # not by a check-then-use against the provider.
        provider._fail_session(old_session, "late over-limit")
        provider._deliver_page(old_session, {"port": 1111, "target_id": "old-target"})
        old_pending = _PendingRequest(request_id="old", method="send")
        with old_session.lock:
            old_session.pending = old_pending
        provider._deliver_response(old_session, {"id": "old", "ok": True, "result": "1"})
        old_tail.append("old-diagnostic")
        self.assertEqual(fresh_session.terminal_error, "")
        self.assertEqual(fresh_session.cdp_port, 0)
        self.assertEqual(fresh_session.target_id, "")
        self.assertIsNone(fresh_session.pending)
        self.assertEqual(list(fresh_session.stderr_tail), [])
        # The old session did record on itself only.
        self.assertIn("late", old_session.terminal_error)

    def test_page_event_is_per_session(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.stdout = _RecordedStringIO(
            '{"event":"page","port":9222,"target_id":"t-1"}\n'
        )
        proc.poll.return_value = 1
        session = self._session_for(provider, proc)
        provider._read_loop(session)
        self.assertEqual(session.cdp_port, 9222)
        self.assertEqual(session.target_id, "t-1")

    def test_waiter_on_replaced_session_ends_promptly(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        old = mock.Mock()
        old.poll.return_value = None
        old.stdin = mock.Mock()
        session = self._session_for(provider, old)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        errors: list[BaseException] = []

        def do_wait() -> None:
            try:
                provider._wait_for_response(session, pending, self._deadline(30.0))
            except BaseException as exc:
                errors.append(exc)

        waiter = threading.Thread(target=do_wait)
        waiter.start()
        # Let the waiter park in its short wait, then close: close wakes
        # the pending slot, so the waiter ends promptly with exited.
        deadline = time.monotonic() + 10.0
        while not waiter.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
        ):
            provider.close()
        waiter.join(timeout=10.0)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ProviderActionError)
        self.assertIn("exited", errors[0].failure.message)

    def test_close_never_restarts_a_dead_worker(self) -> None:
        provider = self._provider()
        with mock.patch.object(
            WorkerChatProvider, "_start_session_locked",
            side_effect=AssertionError("close() must not restart"),
        ):
            provider.close()  # _session is None
        self.assertIsNone(provider._session)

    def test_close_never_restarts_an_exited_worker(self) -> None:
        provider = self._provider()
        exited = mock.Mock()
        exited.poll.return_value = 1
        exited.stdin = mock.Mock()
        self._session_for(provider, exited)
        with (
            mock.patch.object(
                WorkerChatProvider, "_start_session_locked",
                side_effect=AssertionError("close() must not restart"),
            ),
            mock.patch("codey.providers.worker.cancellation.terminate_process_tree", side_effect=self._mark_process_exited),
        ):
            provider.close()
        self.assertIsNone(provider._session)

    def test_close_sends_nothing_and_never_restarts(self) -> None:
        # close() detaches under the life lock only: no request is sent
        # and no session is started, and close never blocks on a long send
        # holding the request gate.
        provider = self._provider()
        with (
            mock.patch.object(
                WorkerChatProvider, "_request_locked",
                side_effect=AssertionError("close() must not send requests"),
            ),
            mock.patch.object(
                WorkerChatProvider, "_start_session_locked",
                side_effect=AssertionError("close() must not restart"),
            ),
            mock.patch.object(provider, "_terminate_session") as terminate,
        ):
            proc = mock.Mock()
            self._session_for(provider, proc)
            provider.close()
        terminate.assert_called_once()

    def test_close_does_not_wait_for_inflight_request_gate(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        self._session_for(provider, proc)
        provider._request_lock.acquire()
        try:
            with mock.patch.object(provider, "_terminate_session") as terminate:
                provider.close()
            terminate.assert_called_once()
        finally:
            provider._request_lock.release()

    def test_completed_reply_wins_over_proc_exit(self) -> None:
        # A verdict that arrived before the exit must not be rewritten
        # into "provider worker exited" by an exit check.
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = 0
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
            pending.response = {"id": "req-1", "ok": True, "result": "done"}
            pending.done.set()
        result = provider._wait_for_response(session, pending, self._deadline())
        self.assertEqual(result, "done")
        with session.lock:
            self.assertIsNone(session.pending)

    def test_exit_drains_buffered_reply(self) -> None:
        # The child is already gone but its reply is still in the pipe:
        # the waiter drains briefly instead of failing the completed op.
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.stdin = mock.Mock()
        proc.stdout = _RecordedStringIO('{"id":"req-1","ok":true,"result":"late"}\n')
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        reader = threading.Thread(
            target=provider._read_loop, args=(session,), daemon=True,
        )
        reader.start()
        result = provider._wait_for_response(session, pending, self._deadline())
        reader.join(timeout=10.0)
        self.assertEqual(result, "late")
        self.assertFalse(reader.is_alive())

    def test_reply_wins_over_close_replacement(self) -> None:
        # Valid reply in pending, then a non-Stop close retires the session:
        # the waiter must return the reply, not "provider worker exited".
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
            pending.response = {"id": "req-1", "ok": True, "result": "done"}
            pending.done.set()
        fresh = mock.Mock()
        fresh.poll.return_value = None
        fresh.stdin = mock.Mock()
        with provider._life_lock:
            with session.lock:
                session.closed = True
            provider._session = _WorkerSession(proc=fresh, job=None)
        result = provider._wait_for_response(session, pending, self._deadline())
        self.assertEqual(result, "done")
        assert provider._session is not None
        self.assertIs(provider._session.proc, fresh)

    def test_write_wait_fails_fast_when_owned_process_and_reader_exit(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        session.stdout_done.set()
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending

        with (
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ) as terminate,
            mock.patch.object(
                session.write_done, "wait",
                side_effect=AssertionError("exited worker must not wait for stdin"),
            ) as wait,
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._await_write(session, pending, self._deadline(30.0))
        self.assertIn("exited", raised.exception.failure.message)
        wait.assert_not_called()
        proc.poll.assert_called()
        terminate.assert_called_once_with(proc, None)

    def test_reply_before_write_done_still_wins(self) -> None:
        # The reply proves delivery even when the writer has not yet set
        # write_done; a late write error must not negate it.
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
            session.write_done.clear()
        provider._deliver_response(
            session, {"id": "req-1", "ok": True, "result": "early"},
        )
        provider._await_write(session, pending, self._deadline())
        result = provider._wait_for_response(session, pending, self._deadline())
        self.assertEqual(result, "early")
        with session.lock:
            session.write_error = "provider worker stdin is unavailable: late"
            session.write_done.set()
        provider._await_write(session, pending, self._deadline())

    def _wait_for_pending(
        self, session: _WorkerSession, timeout: float = 10.0
    ) -> _PendingRequest:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with session.lock:
                pending = session.pending
                if pending is not None:
                    return pending
            time.sleep(0.01)
        raise AssertionError("pending was never registered")

    def test_consecutive_requests_share_session_after_early_reply(self) -> None:
        # Real writer interleave: first reply wins before write_done, the
        # writer then succeeds, and the second real request reuses the same
        # session instead of hitting the invariant.
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        entered = threading.Event()
        release = threading.Event()

        def gated_flush() -> None:
            entered.set()
            assert release.wait(timeout=10.0)

        proc.stdin.flush.side_effect = gated_flush
        first_out: dict[str, object] = {}
        first_errors: list[BaseException] = []

        def first_request() -> None:
            try:
                first_out["result"] = provider._request(
                    "send", {"text": "first"}, 5.0
                )
            except BaseException as exc:
                first_errors.append(exc)

        worker = threading.Thread(target=first_request, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=10.0))
        first_pending = self._wait_for_pending(session)
        provider._deliver_response(
            session,
            {"id": first_pending.request_id, "ok": True, "result": "first-ok"},
        )
        worker.join(timeout=10.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_out.get("result"), "first-ok")
        # Previous pending cleared but its writer still holds the slot.
        with session.lock:
            self.assertIsNone(session.pending)
            self.assertIsNotNone(session.write_wire)
        release.set()

        second_out: dict[str, object] = {}
        second_errors: list[BaseException] = []

        def second_request() -> None:
            try:
                second_out["result"] = provider._request(
                    "send", {"text": "second"}, 5.0
                )
            except BaseException as exc:
                second_errors.append(exc)

        second_worker = threading.Thread(target=second_request, daemon=True)
        second_worker.start()
        second_pending = self._wait_for_pending(session)
        provider._deliver_response(
            session,
            {"id": second_pending.request_id, "ok": True, "result": "second-ok"},
        )
        second_worker.join(timeout=10.0)
        self.assertFalse(second_worker.is_alive())
        self.assertEqual(second_errors, [])
        self.assertEqual(second_out.get("result"), "second-ok")
        self.assertIs(provider._session, session)

    def test_late_write_failure_uses_new_session_for_next_request(self) -> None:
        # Real writer clears the slot with a failure after the first reply
        # already succeeded. The sticky terminal must retire the old
        # generation so the next real request starts clean.
        provider = self._provider()
        old = mock.Mock()
        old.poll.return_value = None
        old.stdin = mock.Mock()
        session = self._session_for(provider, old)
        entered = threading.Event()
        release = threading.Event()

        def failing_flush() -> None:
            entered.set()
            assert release.wait(timeout=10.0)
            raise OSError("broken pipe")

        old.stdin.flush.side_effect = failing_flush
        first_out: dict[str, object] = {}
        first_errors: list[BaseException] = []

        def first_request() -> None:
            try:
                first_out["result"] = provider._request(
                    "send", {"text": "first"}, 5.0
                )
            except BaseException as exc:
                first_errors.append(exc)

        worker = threading.Thread(target=first_request, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=10.0))
        first_pending = self._wait_for_pending(session)
        provider._deliver_response(
            session,
            {"id": first_pending.request_id, "ok": True, "result": "first-ok"},
        )
        worker.join(timeout=10.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(first_out.get("result"), "first-ok")
        release.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with session.lock:
                if session.write_wire is None and session.write_done.is_set():
                    break
            time.sleep(0.01)
        with session.lock:
            self.assertIsNone(session.write_wire)
            self.assertNotEqual(session.write_error, "")
            self.assertNotEqual(session.terminal_error, "")
        fresh = mock.Mock()
        fresh.poll.return_value = None
        fresh.stdin = mock.Mock()

        def fake_start(inner_self) -> None:
            fresh_session = _WorkerSession(proc=fresh, job=None)
            inner_self._session = fresh_session
            self._spawn_writer(inner_self, fresh_session)

        second_out: dict[str, object] = {}
        second_errors: list[BaseException] = []

        def second_request() -> None:
            try:
                second_out["result"] = provider._request(
                    "send", {"text": "second"}, 5.0
                )
            except BaseException as exc:
                second_errors.append(exc)

        with (
            mock.patch.object(
                WorkerChatProvider, "_start_session_locked", fake_start
            ),
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ) as terminate,
        ):
            second_worker = threading.Thread(target=second_request, daemon=True)
            second_worker.start()
            fresh_session = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                current = provider._session
                if current is not None and current is not session:
                    fresh_session = current
                    try:
                        self._wait_for_pending(fresh_session, timeout=1.0)
                        break
                    except AssertionError:
                        pass
                time.sleep(0.01)
            assert fresh_session is not None
            second_pending = self._wait_for_pending(fresh_session)
            provider._deliver_response(
                fresh_session,
                {"id": second_pending.request_id, "ok": True, "result": "second-ok"},
            )
            second_worker.join(timeout=10.0)
        self.assertFalse(second_worker.is_alive())
        self.assertEqual(second_errors, [])
        self.assertEqual(second_out.get("result"), "second-ok")
        self.assertEqual(first_out.get("result"), "first-ok")
        terminate.assert_called_once_with(old, None)
        assert provider._session is not None
        self.assertIs(provider._session.proc, fresh)

    def test_slot_wait_timeout_retires_stuck_session(self) -> None:
        # A previous writer that never releases: the second request times
        # out, retires the stuck generation, and a third request starts new.
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        old = mock.Mock()
        old.poll.return_value = None
        old.stdin = mock.Mock()
        session = self._session_for(provider, old)
        with session.lock:
            session.pending = None
            session.write_wire = "stuck-wire"
            session.write_error = ""
            session.write_done.clear()
        with (
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._request_locked("send", {"text": "stuck"}, time.monotonic() + 0.3)
        self.assertIn("timed out", raised.exception.failure.message)
        self.assertTrue(session.closed)
        assert provider._session is None
        fresh = mock.Mock()
        fresh.poll.return_value = None
        fresh.stdin = mock.Mock()

        def fake_start(inner_self) -> None:
            fresh_session = _WorkerSession(proc=fresh, job=None)
            inner_self._session = fresh_session
            self._spawn_writer(inner_self, fresh_session)

        third_out: dict[str, object] = {}

        def third_request() -> None:
            third_out["result"] = provider._request("send", {"text": "third"}, 5.0)

        with (
            mock.patch.object(
                WorkerChatProvider, "_start_session_locked", fake_start
            ),
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
        ):
            third_worker = threading.Thread(target=third_request, daemon=True)
            third_worker.start()
            fresh_session = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                current = provider._session
                if current is not None and current is not session:
                    fresh_session = current
                    try:
                        self._wait_for_pending(fresh_session, timeout=1.0)
                        break
                    except AssertionError:
                        pass
                time.sleep(0.01)
            assert fresh_session is not None
            third_pending = self._wait_for_pending(fresh_session)
            provider._deliver_response(
                fresh_session,
                {"id": third_pending.request_id, "ok": True, "result": "third-ok"},
            )
            third_worker.join(timeout=10.0)
        self.assertFalse(third_worker.is_alive())
        self.assertEqual(third_out.get("result"), "third-ok")
        assert provider._session is not None
        self.assertIs(provider._session.proc, fresh)

    def test_write_failure_first_request_retires_for_next(self) -> None:
        # No reply arrives and the real writer fails: the first request
        # raises exactly once and the condemned generation is never reused.
        provider = self._provider()
        old = mock.Mock()
        old.poll.return_value = None
        old.stdin = mock.Mock()
        session = self._session_for(provider, old)
        entered = threading.Event()
        release = threading.Event()

        def failing_flush() -> None:
            entered.set()
            assert release.wait(timeout=10.0)
            raise OSError("broken pipe")

        old.stdin.flush.side_effect = failing_flush
        first_errors: list[BaseException] = []

        def first_request() -> None:
            try:
                provider._request("send", {"text": "first"}, 5.0)
            except BaseException as exc:
                first_errors.append(exc)

        worker = threading.Thread(target=first_request, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=10.0))
        release.set()
        worker.join(timeout=10.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(first_errors), 1)
        from codey.providers.diagnostics import ProviderActionError

        self.assertIsInstance(first_errors[0], ProviderActionError)
        assert isinstance(first_errors[0], ProviderActionError)
        self.assertIn("broken pipe", first_errors[0].failure.message)
        with session.lock:
            self.assertNotEqual(session.terminal_error, "")
            self.assertIsNone(session.pending)
        # The test process is a Mock; after writer-triggered teardown, model
        # the child exit that a real Popen would report.
        old.poll.return_value = 0
        fresh = mock.Mock()
        fresh.poll.return_value = None
        fresh.stdin = mock.Mock()

        def fake_start(inner_self) -> None:
            fresh_session = _WorkerSession(proc=fresh, job=None)
            inner_self._session = fresh_session
            self._spawn_writer(inner_self, fresh_session)

        second_out: dict[str, object] = {}

        def second_request() -> None:
            second_out["result"] = provider._request("send", {"text": "second"}, 5.0)

        with (
            mock.patch.object(
                WorkerChatProvider, "_start_session_locked", fake_start
            ),
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
        ):
            second_worker = threading.Thread(target=second_request, daemon=True)
            second_worker.start()
            fresh_session = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                current = provider._session
                if current is not None and current is not session:
                    fresh_session = current
                    try:
                        self._wait_for_pending(fresh_session, timeout=1.0)
                        break
                    except AssertionError:
                        pass
                time.sleep(0.01)
            assert fresh_session is not None
            second_pending = self._wait_for_pending(fresh_session)
            provider._deliver_response(
                fresh_session,
                {"id": second_pending.request_id, "ok": True, "result": "second-ok"},
            )
            second_worker.join(timeout=10.0)
        self.assertFalse(second_worker.is_alive())
        self.assertEqual(second_out.get("result"), "second-ok")
        assert provider._session is not None
        self.assertIs(provider._session.proc, fresh)

    def test_close_reports_cleanup_completion(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        waiter_errors: list[BaseException] = []

        def do_wait() -> None:
            try:
                provider._wait_for_response(session, pending, self._deadline(30.0))
            except BaseException as exc:
                waiter_errors.append(exc)

        waiter = threading.Thread(target=do_wait, daemon=True)
        waiter.start()
        deadline = time.monotonic() + 10.0
        while not waiter.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
        ):
            self.assertTrue(provider.close())
        waiter.join(timeout=10.0)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(waiter_errors), 1)
        self.assertIsInstance(waiter_errors[0], ProviderActionError)
        self.assertIn("exited", waiter_errors[0].failure.message)
        self.assertIsNone(provider._session)

    def test_close_reports_incomplete_cleanup(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        release_reader = threading.Event()

        class _StuckStdout:
            def readline(self, size: int = -1) -> str:
                assert release_reader.wait(timeout=30.0)
                return ""

            def close(self) -> None:
                return None

        proc.stdout = _StuckStdout()
        session = self._session_for(provider, proc)
        reader = threading.Thread(
            target=provider._read_loop, args=(session,), daemon=True,
        )
        session.reader = reader
        reader.start()
        deadline = time.monotonic() + 10.0
        while not reader.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            with mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ):
                self.assertFalse(provider.close())
            self.assertIs(provider._session, session)
            self.assertTrue(reader.is_alive())
            with mock.patch.object(provider, "_start_session_locked") as start:
                with provider._life_lock, self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                    provider._ensure_live_session_locked()
                start.assert_not_called()
            self.assertFalse(provider.close())
        finally:
            release_reader.set()
            reader.join(timeout=10.0)
        self.assertFalse(reader.is_alive())
        self.assertTrue(provider.close())
        self.assertIsNone(provider._session)

    def test_close_keeps_profile_owned_until_child_exits(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        with mock.patch("codey.providers.worker.cancellation.terminate_process_tree"):
            self.assertFalse(provider.close())
        self.assertIs(provider._session, session)
        with mock.patch.object(provider, "_start_session_locked") as start:
            with provider._life_lock, self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                provider._ensure_live_session_locked()
            start.assert_not_called()
        proc.poll.return_value = 0
        self.assertTrue(provider.close())
        self.assertIsNone(provider._session)

    def test_second_close_retries_live_child_and_releases(self) -> None:
        # First teardown leaves the direct child alive: a second close
        # must retry the bounded terminate instead of pinning the old
        # generation forever. An exited child is never signalled again.
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        proc.stdout = mock.Mock()
        proc.stderr = mock.Mock()
        session = self._session_for(provider, proc)
        calls: list[str] = []

        def _first_noop(_proc: object, _job: object) -> None:
            calls.append("first")

        def _second_exit(_proc: object, _job: object) -> None:
            calls.append("second")
            proc.poll.return_value = 0

        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
            side_effect=_first_noop,
        ):
            self.assertFalse(provider.close())
        self.assertIs(provider._session, session)
        self.assertEqual(calls, ["first"])
        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
            side_effect=_second_exit,
        ):
            self.assertTrue(provider.close())
        self.assertIsNone(provider._session)
        self.assertEqual(calls, ["first", "second"])

    def test_second_close_does_not_resignal_exited_child(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        with mock.patch("codey.providers.worker.cancellation.terminate_process_tree"):
            self.assertFalse(provider.close())
        self.assertIs(provider._session, session)
        proc.poll.return_value = 0
        with mock.patch(
            "codey.providers.worker.cancellation.terminate_process_tree",
            side_effect=AssertionError("exited child must not be resignalled"),
        ):
            self.assertTrue(provider.close())
        self.assertIsNone(provider._session)

    def test_terminate_skips_live_reader_pipe(self) -> None:
        # Closing a live reader's pipe would block on its IO lock; the
        # retire path must join first and leave the pipe abandoned.
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.stdin = mock.Mock()
        closed: list[str] = []
        proc.stdout = SimpleNamespace(close=lambda: closed.append("stdout"))
        proc.stderr = None
        session = self._session_for(provider, proc)
        release = threading.Event()

        def _stuck_reader() -> None:
            assert release.wait(timeout=30.0)

        stuck = threading.Thread(target=_stuck_reader, daemon=True)
        session.reader = stuck
        stuck.start()
        deadline = time.monotonic() + 10.0
        while not stuck.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            with mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree"
            ):
                started = time.monotonic()
                self.assertFalse(provider.close())
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 10.0)
            self.assertNotIn("stdout", closed)
            self.assertTrue(stuck.is_alive())
        finally:
            release.set()
            stuck.join(timeout=10.0)
        self.assertFalse(stuck.is_alive())
        self.assertTrue(provider.close())
        self.assertIsNone(provider._session)

    def test_closed_generation_check_does_not_join_past_request_deadline(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        with mock.patch("codey.providers.worker.cancellation.terminate_process_tree"):
            self.assertFalse(provider.close())
        with (
            mock.patch.object(provider, "_join_threads", side_effect=AssertionError("must not wait")),
            mock.patch.object(provider, "_start_session_locked") as start,
            self.assertRaisesRegex(RuntimeError, "cleanup incomplete"),
        ):
            provider._request_locked("send", {"text": "next"}, time.monotonic() + 0.1)
        start.assert_not_called()
        self.assertIs(provider._session, session)
        proc.poll.return_value = 0
        self.assertTrue(provider.close())

    def test_session_start_cannot_send_after_deadline(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)

        def slow_ensure() -> _WorkerSession:
            time.sleep(0.03)
            return session

        with (
            mock.patch.object(provider, "_ensure_live_session_locked", side_effect=slow_ensure),
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._request_locked("send", {"text": "late"}, time.monotonic() + 0.01)
        self.assertIn("timed out", raised.exception.failure.message)
        self.assertIsNone(session.pending)
        proc.stdin.write.assert_not_called()
        proc.poll.return_value = 0
        self.assertTrue(provider.close())

    def test_stop_during_session_start_retires_unused_worker(self) -> None:
        from codey.runtime.core import cancellation as cancel

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        stop = threading.Event()

        def stop_during_ensure() -> _WorkerSession:
            stop.set()
            return session

        with (
            cancel.scope(stop),
            mock.patch.object(provider, "_ensure_live_session_locked", side_effect=stop_during_ensure),
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
            self.assertRaises(cancel.TaskCancelled),
        ):
            provider._request_locked("send", {"text": "never"}, time.monotonic() + 1.0)
        proc.stdin.write.assert_not_called()
        self.assertIsNone(provider._session)

    def test_stop_during_request_gate_is_prompt(self) -> None:
        from codey.runtime.core import cancellation as cancel

        provider = self._provider()
        provider._request_lock.acquire()
        try:
            stop = threading.Event()
            errors: list[BaseException] = []

            def do_request() -> None:
                try:
                    with cancel.scope(stop):
                        provider._request("send", {"text": "hi"}, 30.0)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=do_request, daemon=True)
            worker.start()
            time.sleep(0.2)
            stop.set()
            worker.join(timeout=5.0)
        finally:
            with contextlib.suppress(Exception):
                provider._request_lock.release()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], cancel.TaskCancelled)

    def test_exited_without_reply_fails_fast(self) -> None:
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = 1
        proc.stdin = mock.Mock()
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        session.stdout_done.set()
        started = time.monotonic()
        with (
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._wait_for_response(session, pending, self._deadline(30.0))
        self.assertIn("exited", raised.exception.failure.message)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_bare_ok_true_is_malformed_send_reply(self) -> None:
        # {"ok": true} without a string result must not degrade to "".
        from codey.providers.diagnostics import ProviderActionError

        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        provider._deliver_response(session, {"id": "req-1", "ok": True})
        self.assertIn("malformed", session.terminal_error)
        with (
            mock.patch(
                "codey.providers.worker.cancellation.terminate_process_tree",
                side_effect=self._mark_process_exited,
            ),
            self.assertRaises(ProviderActionError) as raised,
        ):
            provider._wait_for_response(session, pending, self._deadline())
        self.assertIn("malformed", raised.exception.failure.message)

    def test_duplicate_reply_keeps_first_verdict(self) -> None:
        provider = self._provider()
        proc = mock.Mock()
        proc.poll.return_value = None
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        provider._deliver_response(
            session, {"id": "req-1", "ok": True, "result": "first"},
        )
        provider._deliver_response(
            session, {"id": "req-1", "ok": True, "result": "second"},
        )
        self.assertEqual(pending.response["result"], "first")
        self.assertIn("duplicate", session.terminal_error)

    def test_max_frame_succeeds_through_waiter(self) -> None:
        # The MAX-chars-plus-newline frame must survive the full path, not
        # just a reader-only unit check that never races the exit check.
        provider = self._provider()
        prefix = '{"id":"req-1","ok":true,"result":"'
        suffix = '"}\n'
        line = prefix + "p" * (WORKER_LINE_MAX_CHARS - len(prefix) - len(suffix) + 1) + suffix
        self.assertEqual(len(line), WORKER_LINE_MAX_CHARS + 1)
        release = threading.Event()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()

        class _OneLineThenPark:
            sizes: list[int] = []

            def __init__(self) -> None:
                self._sent = False

            def readline(self, size: int = -1) -> str:
                self.sizes.append(size)
                if not self._sent:
                    self._sent = True
                    return line
                assert release.wait(timeout=10.0)
                return ""

        proc.stdout = _OneLineThenPark()
        session = self._session_for(provider, proc)
        pending = _PendingRequest(request_id="req-1", method="send")
        with session.lock:
            session.pending = pending
        reader = threading.Thread(
            target=provider._read_loop, args=(session,), daemon=True,
        )
        try:
            reader.start()
            result = provider._wait_for_response(session, pending, self._deadline())
            self.assertEqual(result, "p" * (WORKER_LINE_MAX_CHARS - len(prefix) - len(suffix) + 1))
        finally:
            release.set()
            reader.join(timeout=10.0)
        self.assertFalse(reader.is_alive())

    def test_real_process_terminate_kills_tree(self) -> None:
        import subprocess as _subprocess
        import sys as _sys

        provider = self._provider()
        # Helper contract: the child owns its process group (production
        # start_process uses start_new_session=True), so terminate signals
        # the group leader instead of an arbitrary external child.
        proc = _subprocess.Popen(
            [_sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            start_new_session=True,
        )
        try:
            session = _WorkerSession(proc=proc, job=None)  # type: ignore[arg-type]
            provider._session = session
            provider.close()
            self.assertIsNone(provider._session)
            self.assertIsNotNone(proc.poll())
        finally:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                with contextlib.suppress(Exception):
                    if stream is not None:
                        stream.close()


class ProviderProbeErrorTests(unittest.TestCase):
    def test_probe_crash_is_signalled_not_silent(self) -> None:
        ctx = SimpleNamespace()
        with (
            mock.patch(
                "codey.app.api.provider_services.provider_availability",
                side_effect=RuntimeError("probe exploded"),
            ),
            self.assertLogs("codey.app.api", level="ERROR") as captured,
        ):
            status, payload = app_api.providers_response(ctx)
        self.assertEqual(status, 200)
        self.assertTrue(payload["probe_error"])
        self.assertTrue(all(item["available"] is False for item in payload["providers"]))
        self.assertTrue(any("probe" in message for message in captured.output))

    def test_healthy_probe_reports_no_error(self) -> None:
        ctx = SimpleNamespace()
        with mock.patch(
            "codey.app.api.provider_services.provider_availability",
            return_value={"deepseek": True},
        ):
            status, payload = app_api.providers_response(ctx)
        self.assertEqual(status, 200)
        self.assertFalse(payload["probe_error"])

    def test_providers_response_carries_backend_catalog(self) -> None:
        ctx = SimpleNamespace()
        with mock.patch(
            "codey.app.api.provider_services.provider_availability",
            return_value={},
        ):
            status, payload = app_api.providers_response(ctx)
        self.assertEqual(status, 200)
        self.assertEqual(payload["default"], DEFAULT_PROVIDER_ID)
        self.assertEqual(
            [item["id"] for item in payload["providers"]],
            list(PROVIDER_LABELS),
        )
        self.assertTrue(all("label" in item for item in payload["providers"]))

    def test_provider_catalog_never_probes(self) -> None:
        with (
            mock.patch(
                "codey.app.api.provider_services.provider_availability",
                side_effect=AssertionError("catalog must not probe"),
            ),
            mock.patch(
                "codey.app.api.provider_services.provider_tab_availability",
                side_effect=AssertionError("catalog must not probe"),
            ),
        ):
            status, payload = app_api.provider_catalog_response()
        self.assertEqual(status, 200)
        self.assertEqual(payload["default"], DEFAULT_PROVIDER_ID)
        self.assertEqual(
            [item["id"] for item in payload["providers"]],
            list(PROVIDER_LABELS),
        )
        self.assertTrue(all("label" in item for item in payload["providers"]))
        self.assertTrue(all("available" not in item for item in payload["providers"]))


class ConversationPruneTests(unittest.TestCase):
    def test_prune_skips_unstatable_files_instead_of_abandoning(self) -> None:
        from codey.storage.file_lock import with_file_lock

        with tempfile.TemporaryDirectory() as td:
            store = ConversationStore(Path(td))
            store.directory.mkdir(parents=True, exist_ok=True)
            keep = store.directory / "keep.json"
            keep.write_text("{}", encoding="utf-8")
            victims: list[Path] = []
            for index in range(70):
                path = store.directory / f"c{index:03d}.json"
                path.write_text("{}", encoding="utf-8")
                victims.append(path)
            victim = store.directory / "c000.json"
            real_stat = Path.stat

            def flaky_stat(self: Path, *args: object, **kwargs: object):
                if self.name == victim.name:
                    raise OSError("disk hiccup")
                return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

            with mock.patch.object(Path, "stat", flaky_stat), with_file_lock(store.directory):
                store._prune_locked(keep)  # must not raise
            remaining = list(store.directory.glob("*.json"))
            # 63 newest stat-able + keep + the un-statable survivor.
            self.assertEqual(len(remaining), 65)
            self.assertTrue(victim.exists())


class ResearchUrlKeyTests(unittest.TestCase):
    def test_result_ids_use_normalized_url_keys(self) -> None:
        controller = ResearchController()
        ledger = SimpleNamespace(searches=[
            SimpleNamespace(results=[
                SimpleNamespace(url="HTTPS://Example.COM/a/", title="A", snippet="x"),
                SimpleNamespace(url="https://www.example.com/a", title="A", snippet="x"),
                SimpleNamespace(url="https://example.com/a", title="A", snippet="x"),
            ]),
        ])
        rows = controller._result_rows(ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "r1")


class HeadlessShellExpiryTests(unittest.TestCase):
    def test_shell_request_expires_pending_approvals(self) -> None:
        rows: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as td:
            ctx = HeadlessAppContext(
                Path(td, "state"),
                port=19222,
                emit_jsonl=rows.append,
            )
            ctx.add_pending_shell_approval("appr-1", {
                "id": "appr-1",
                "run_id": "r1",
                "session_id": "s1",
                "command": "pytest -q",
                "cwd": ".",
            })
            ctx.emit({
                "type": "shell_request",
                "run_id": "r1",
                "session_id": "s1",
                "id": "appr-1",
                "command": "pytest -q",
                "cwd": ".",
            })
            pending = ctx.pending_shell_approvals()
        self.assertTrue(ctx.shell_rejected)
        self.assertTrue(ctx.run_registry.stop_flag.is_set())
        self.assertEqual(pending, {})
        self.assertTrue(any(row.get("type") == "shell_rejected" for row in rows))


class PostBodyTimeoutTests(unittest.TestCase):
    def test_slow_body_returns_408(self) -> None:
        from codey.app.server import Handler

        handler = Handler.__new__(Handler)
        handler.rfile = SimpleNamespace(read=mock.Mock(side_effect=socket.timeout))
        handler.connection = SimpleNamespace(
            gettimeout=mock.Mock(return_value=None),
            settimeout=mock.Mock(),
        )
        sent: list[tuple[int, dict]] = []
        handler._send_json = lambda status, payload: sent.append((status, payload))  # type: ignore[method-assign]
        self.assertIsNone(handler._read_post_body(16))
        self.assertEqual(sent[0][0], 408)


class AppContextLifecycleTests(unittest.TestCase):
    def test_app_context_has_no_sync_ghost_flag_and_waits_explicitly(self) -> None:
        ctx_default = AppContext()
        try:
            self.assertFalse(hasattr(ctx_default, "sync_ghost_maintenance"))
            self.assertTrue(ctx_default.wait_for_ghost_sleep(timeout=0.1))
        finally:
            ctx_default.close()

    def test_app_context_ghost_wait_is_always_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            custom_home = Path(td, "state")
            ctx = AppContext(custom_home)
            try:
                self.assertFalse(hasattr(ctx, "sync_ghost_maintenance"))
            finally:
                ctx.close()

    def test_app_context_close_is_explicit_and_returns_completion(self) -> None:
        store_mock = mock.Mock()
        ctx = AppContext()
        try:
            ctx._knowledge_store = store_mock
            with mock.patch.object(
                ctx.ghost_sleep_daemon, "wait", return_value=True,
            ) as daemon_wait:
                ephemeral = ctx._ephemeral_runtime_home
                self.assertIsNotNone(ephemeral)
                self.assertTrue(ctx.close())
                self.assertTrue(ctx.closed)
        finally:
            ctx._resources_closed = True
        daemon_wait.assert_called_once()
        store_mock.close.assert_called_once()

    def test_app_context_has_no_context_manager(self) -> None:
        # Shutdown incompleteness is a value (close() -> bool), not an
        # exception: a context manager would silently swallow False.
        self.assertFalse(hasattr(AppContext, "__enter__"))
        self.assertFalse(hasattr(AppContext, "__exit__"))

    def test_app_context_close_retains_resources_when_ghost_alive(self) -> None:
        store_mock = mock.Mock()
        ctx = AppContext()
        try:
            ctx._knowledge_store = store_mock
            # A live Ghost owns its stores: close() retains everything and
            # reports incomplete instead of releasing files under the
            # daemon (the Windows .lock handle failure).
            with (
                mock.patch.object(ctx.ghost_sleep_daemon, "wait", return_value=False),
                mock.patch.object(ctx._ephemeral_runtime_home, "cleanup") as cleanup_mock,
            ):
                assert ctx._ephemeral_runtime_home is not None
                self.assertFalse(ctx.close())
                cleanup_mock.assert_not_called()
                store_mock.close.assert_not_called()
                self.assertTrue(ctx.run_registry.stop_flag.is_set())
                self.assertFalse(ctx.closed)
                self.assertTrue(ctx._close_requested)
            # Retry after the daemon finishes closes exactly once.
            with (
                mock.patch.object(ctx.ghost_sleep_daemon, "wait", return_value=True),
                mock.patch.object(ctx._ephemeral_runtime_home, "cleanup") as cleanup_mock,
            ):
                self.assertTrue(ctx.close())
                cleanup_mock.assert_called_once()
                store_mock.close.assert_called_once()
                self.assertTrue(ctx.closed)
                # Fully closed: further calls are idempotent.
                self.assertTrue(ctx.close())
                store_mock.close.assert_called_once()
        finally:
            ctx._resources_closed = True

    def test_app_context_close_cleans_resources_when_finished(self) -> None:
        store_mock = mock.Mock()
        evidence_mock = mock.Mock()
        ctx = AppContext()
        try:
            ctx._knowledge_store = store_mock
            ctx.evidence_ledgers = evidence_mock
            with mock.patch.object(ctx.ghost_sleep_daemon, "wait", return_value=True):
                assert ctx._ephemeral_runtime_home is not None
                with mock.patch.object(ctx._ephemeral_runtime_home, "cleanup") as cleanup_mock:
                    self.assertTrue(ctx.close())
                    store_mock.close.assert_called_once()
                    evidence_mock.close.assert_called_once()
                    cleanup_mock.assert_called_once()
                    self.assertTrue(ctx.run_registry.stop_flag.is_set())
                    self.assertTrue(ctx.closed)
                    self.assertTrue(ctx._resources_closed)
        finally:
            ctx._resources_closed = True

    def test_app_context_close_retries_after_ghost_finishes(self) -> None:
        store_mock = mock.Mock()
        evidence_mock = mock.Mock()
        ctx = AppContext()
        try:
            ctx._knowledge_store = store_mock
            ctx.evidence_ledgers = evidence_mock
            assert ctx._ephemeral_runtime_home is not None
            with mock.patch.object(ctx._ephemeral_runtime_home, "cleanup") as cleanup_mock:
                # 1. First close: daemon still running, nothing released.
                with mock.patch.object(ctx.ghost_sleep_daemon, "wait", return_value=False):
                    self.assertFalse(ctx.close())
                    cleanup_mock.assert_not_called()
                    store_mock.close.assert_not_called()
                    evidence_mock.close.assert_not_called()
                    self.assertTrue(ctx._close_requested)
                    self.assertFalse(ctx._resources_closed)
                    self.assertFalse(ctx.closed)

                # 2. Second close after the daemon finishes releases once.
                with mock.patch.object(ctx.ghost_sleep_daemon, "wait", return_value=True):
                    self.assertTrue(ctx.close())
                    cleanup_mock.assert_called_once()
                    store_mock.close.assert_called_once()
                    evidence_mock.close.assert_called_once()
        finally:
            ctx._resources_closed = True

    def test_app_context_close_retries_cleanup_failures(self) -> None:
        store_mock = mock.Mock()
        evidence_mock = mock.Mock()
        ctx = AppContext()
        try:
            ctx._knowledge_store = store_mock
            ctx.evidence_ledgers = evidence_mock
            assert ctx._ephemeral_runtime_home is not None
            with mock.patch.object(ctx.ghost_sleep_daemon, "wait", return_value=True), mock.patch.object(ctx._ephemeral_runtime_home, "cleanup") as cleanup_mock:
                cleanup_mock.side_effect = [PermissionError("locked"), None]

                ctx.close()

                store_mock.close.assert_called_once()
                evidence_mock.close.assert_called_once()
                self.assertIsNone(ctx._knowledge_store)
                self.assertFalse(ctx._knowledge_store_enabled)
                self.assertIsNone(ctx.evidence_ledgers)
                self.assertIsNotNone(ctx._ephemeral_runtime_home)
                self.assertFalse(ctx.closed)

                ctx.close()

                store_mock.close.assert_called_once()
                evidence_mock.close.assert_called_once()
                self.assertEqual(cleanup_mock.call_count, 2)
                self.assertIsNone(ctx._ephemeral_runtime_home)
                self.assertTrue(ctx.closed)
        finally:
            ctx._resources_closed = True


if __name__ == "__main__":
    unittest.main()
