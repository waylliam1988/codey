"""Parent-side JSON-line Provider worker wrapper."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from codey.automation.browser import DEFAULT_PORT
from codey.providers.catalog import WORKER_CHILD_ENV
from codey.providers.diagnostics import (
    FAILURE_RESPONSE_MISSING,
    ProviderActionError,
    ProviderFailure,
)
from codey.repairs.adapter_overrides import AdapterOverride, record_failure, record_success
from codey.runtime.core import cancellation
from codey.storage.local_store import DEFAULT_STATE_HOME

WORKER_TIMEOUT_GRACE = 5.0
# True read bounds: stdout frames with a per-line cap and stderr tails in
# fixed-size chunks, so one huge child line never enters memory whole.
WORKER_LINE_MAX_CHARS = 256 * 1024
WORKER_STDERR_CHUNK_CHARS = 16 * 1024
WORKER_STDERR_TAIL_CHUNKS = 4
READER_JOIN_TIMEOUT = 2.0
# Bounded drain after the child exits: a reply already in the pipe still
# counts, but a silent exit never pins the waiter past this grace.
EXIT_DRAIN_GRACE = 2.0


@dataclass
class _PendingRequest:
    """One in-flight request: first verdict wins, later frames cannot rewrite it."""

    request_id: str
    method: str
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    error: str = ""


@dataclass
class _WorkerSession:
    """One worker generation: proc plus everything that belongs to it.

    Readers and the stdin writer only ever touch their own session object,
    so a late thread from a retired generation cannot fail, misroute, or
    pollute its replacement.
    """

    proc: subprocess.Popen[str]
    job: object
    stderr_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=WORKER_STDERR_TAIL_CHUNKS)
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: _PendingRequest | None = None
    terminal_error: str = ""
    cdp_port: int = 0
    target_id: str = ""
    closed: bool = False
    reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    writer: threading.Thread | None = None
    write_event: threading.Event = field(default_factory=threading.Event)
    write_done: threading.Event = field(default_factory=threading.Event)
    write_wire: str | None = None
    write_error: str = ""
    stdout_done: threading.Event = field(default_factory=threading.Event)


@dataclass
class WorkerChatProvider:
    provider_id: str
    override: AdapterOverride
    port: int = DEFAULT_PORT + 100
    state_home: Path = DEFAULT_STATE_HOME

    def __post_init__(self) -> None:
        self.name = f"{self.provider_id} worker"
        self.last_failure: ProviderFailure | None = None
        # _life_lock serializes session retirement/termination/start so a new
        # generation never reuses the browser profile while the old one is
        # still being torn down. _request_lock keeps requests single-flight.
        # Waiting holds neither lock's critical path: close() marks the
        # current session closed, and the waiter observes that session.
        self._life_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._session: _WorkerSession | None = None
        with self._life_lock:
            self._start_session_locked()

    @property
    def location(self) -> str:
        return f"worker:{self.provider_id}:{self.override.generation}"

    def new_chat(self, timeout: float | None = None) -> None:
        try:
            self._request("new_chat", {"timeout": timeout}, timeout)
        except ProviderActionError as exc:
            record_failure(
                self.provider_id,
                self.override.generation,
                exc.failure.kind,
                state_home=self.state_home,
            )
            raise

    def send(self, text: str, timeout: float | None = None) -> str:
        try:
            result = self._request("send", {"text": text, "timeout": timeout}, timeout)
        except ProviderActionError as exc:
            record_failure(
                self.provider_id,
                self.override.generation,
                exc.failure.kind,
                state_home=self.state_home,
            )
            raise
        record_success(
            self.provider_id,
            self.override.generation,
            state_home=self.state_home,
        )
        return str(result or "")

    def close(self) -> bool:
        # A generation that has not finished cleanup remains owned here.
        # Otherwise another request could reuse its browser profile after a
        # failed close, and a second close would incorrectly report success.
        # Explicit close retries a live direct child; rechecks elsewhere
        # stay fast-fail so short request deadlines never wait.
        with self._life_lock:
            session = self._session
            if session is None:
                return True
            return self._retire_session_locked(session, retry_live_child=True)

    def _start_session_locked(self) -> None:
        """Create, publish, and serve one generation. Holds _life_lock."""
        assert self._session is None
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.override.root) + (os.pathsep + existing if existing else "")
        env[WORKER_CHILD_ENV] = "1"
        # Stable per-provider browser profile: the generation identifies the
        # code override, not the browser identity, so one manual login keeps
        # every later override generation usable without re-auth prompts.
        worker_profile = (
            Path(self.state_home)
            / "provider-workers"
            / self.provider_id
        )
        cmd = [
            sys.executable,
            "-B",
            "-m",
            "codey.providers.worker_child",
            "--provider",
            self.provider_id,
            "--port",
            str(self.port),
            "--profile",
            str(worker_profile),
        ]
        group_args: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.override.root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Drain stderr instead of dropping it: a startup crash must be
            # diagnosable from the parent-side failure message.
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group_args,
        )
        try:
            job = cancellation.attach_process_tree(proc)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
            self._close_all_pipes(proc)
            raise
        session = _WorkerSession(proc=proc, job=job)
        stderr_reader = threading.Thread(
            target=self._stderr_loop, args=(session,), daemon=True,
        )
        reader = threading.Thread(target=self._read_loop, args=(session,), daemon=True)
        writer = threading.Thread(target=self._writer_loop, args=(session,), daemon=True)
        session.stderr_reader = stderr_reader
        session.reader = reader
        session.writer = writer
        started: list[threading.Thread] = []
        try:
            stderr_reader.start()
            started.append(stderr_reader)
            reader.start()
            started.append(reader)
            writer.start()
            started.append(writer)
        except Exception:
            with session.lock:
                session.closed = True
                session.write_event.set()
            with contextlib.suppress(Exception):
                cancellation.terminate_process_tree(proc, job)
            if job is not None:
                with contextlib.suppress(Exception):
                    job.close()  # type: ignore[union-attr]
            self._join_threads(started)
            self._close_stopped_session_pipes(session)
            raise
        # Published only after all three threads run: construction either
        # hands over a fully owned generation or cleans everything itself.
        self._session = session

    def _stderr_loop(self, session: _WorkerSession) -> None:
        # Startup diagnostics only, writing this generation's tail:
        # readline with a chunk cap keeps one huge line from ever entering
        # memory whole, while a short newline-terminated diagnostic lands
        # promptly instead of waiting for a full chunk or EOF. A non-text
        # stream ends the loop instead of spinning forever on truthy junk.
        try:
            stderr = session.proc.stderr
            if stderr is None:
                return
            while True:
                chunk = stderr.readline(WORKER_STDERR_CHUNK_CHARS)
                if not chunk:
                    return
                if not isinstance(chunk, str):
                    with session.lock:
                        if not session.terminal_error:
                            session.terminal_error = (
                                "provider worker stderr is not text"
                            )
                    return
                session.stderr_tail.append(chunk)
        except Exception:
            return

    def _writer_loop(self, session: _WorkerSession) -> None:
        """Own stdin: one blocking write at a time, never holding session.lock."""
        while True:
            session.write_event.wait()
            with session.lock:
                if session.closed:
                    return
                wire = session.write_wire
                if wire is None:
                    session.write_event.clear()
                    continue
            error = ""
            try:
                stdin = session.proc.stdin
                if stdin is None:
                    raise OSError("stdin is unavailable")
                stdin.write(wire)
                stdin.flush()
            except Exception as exc:
                error = f"provider worker stdin is unavailable: {exc}"
            with session.lock:
                if session.closed:
                    return
                session.write_wire = None
                session.write_error = error
                if error and not session.terminal_error:
                    session.terminal_error = error
                session.write_event.clear()
                session.write_done.set()

    def _worker_error_suffix(self, session: _WorkerSession) -> str:
        tail = " | ".join(session.stderr_tail).strip()
        return f": {tail[-400:]}" if tail else ""

    def _read_loop(self, session: _WorkerSession) -> None:
        try:
            proc = session.proc
            stdout = proc.stdout
            if stdout is None:
                self._fail_session(session, "provider worker stdout is unavailable")
                return
            while True:
                try:
                    line = stdout.readline(WORKER_LINE_MAX_CHARS + 1)
                except (OSError, ValueError) as exc:
                    self._fail_session(
                        session, f"provider worker stdout unreadable: {exc}",
                    )
                    return
                if not line:
                    # EOF on a live process means its framing died with no
                    # verdict; a normally exited child leaves the verdict to
                    # the waiter, which drains buffered replies before failing.
                    # stdout_done (set in finally) lets the waiter fail fast
                    # once the reader has drained instead of using the grace.
                    if proc.poll() is None:
                        self._fail_session(
                            session, "provider worker stdout closed unexpectedly",
                        )
                    return
                if not line.endswith("\n"):
                    if proc.poll() is not None:
                        return
                    if len(line) > WORKER_LINE_MAX_CHARS:
                        self._fail_session(
                            session,
                            f"provider worker output exceeded {WORKER_LINE_MAX_CHARS} chars",
                        )
                        return
                    self._fail_session(session, "provider worker sent a malformed frame")
                    return
                # A line of exactly MAX chars plus its newline still fits in
                # one readline(MAX+1); anything longer arrives here without a
                # newline and was condemned above. Never drain, so the next
                # frame is never consumed as this one's tail.
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self._fail_session(session, "provider worker sent a malformed frame")
                    return
                if not isinstance(payload, dict):
                    self._fail_session(session, "provider worker sent a malformed frame")
                    return
                if "event" in payload:
                    # Stdout carries page events and request replies only:
                    # anything else is a protocol failure, never a reply.
                    if payload.get("event") == "page":
                        self._deliver_page(session, payload)
                    else:
                        self._fail_session(
                            session, "provider worker sent an unexpected event",
                        )
                        return
                    continue
                self._deliver_response(session, payload)
        finally:
            session.stdout_done.set()

    def _fail_session(self, session: _WorkerSession, reason: str) -> None:
        """Condemn the generation; complete the waiter once, first verdict wins."""
        with session.lock:
            if session.closed:
                return
            if not session.terminal_error:
                session.terminal_error = reason
            pending = session.pending
            if pending is not None and not pending.done.is_set():
                if pending.response is None and not pending.error:
                    pending.error = reason
                pending.done.set()

    def _deliver_page(self, session: _WorkerSession, payload: dict) -> None:
        with session.lock:
            if session.closed:
                return
            try:
                session.cdp_port = max(0, int(payload.get("port") or 0))
            except (TypeError, ValueError):
                session.cdp_port = 0
            session.target_id = str(payload.get("target_id") or "")

    def _deliver_response(self, session: _WorkerSession, payload: dict) -> None:
        with session.lock:
            if session.closed:
                return
            pending = session.pending
            if pending is None:
                if not session.terminal_error:
                    session.terminal_error = "provider worker sent an unexpected reply"
                return
            if pending.done.is_set():
                # The request already has its verdict: a further frame is a
                # protocol failure of the session, never a rewrite.
                if not session.terminal_error:
                    session.terminal_error = "provider worker sent a duplicate reply"
                return
            if payload.get("id") != pending.request_id:
                reason = "provider worker sent a reply for an unknown request"
                if not session.terminal_error:
                    session.terminal_error = reason
                pending.error = reason
                pending.done.set()
                return
            shape_error = _protocol_error(pending.method, payload)
            if shape_error is not None:
                if not session.terminal_error:
                    session.terminal_error = shape_error
                pending.error = shape_error
                pending.done.set()
                return
            pending.response = payload
            pending.done.set()

    def _ensure_live_session_locked(self) -> _WorkerSession:
        """Return a usable session, restarting a dead/condemned generation."""
        session = self._session
        if (
            session is not None
            and not session.closed
            and not session.terminal_error
            and session.proc.poll() is None
            and session.proc.stdin is not None
        ):
            return session
        if session is not None and not self._retire_session_locked(session):
            raise RuntimeError("provider worker cleanup incomplete")
        self._start_session_locked()
        session = self._session
        if session is None or session.proc.stdin is None:
            raise RuntimeError("provider worker is not running")
        return session

    def _acquire_with_deadline(
        self, lock: threading.Lock, deadline: float, method: str
    ) -> None:
        """Acquire a gate without ever pinning Stop past one short tick.

        Callers hold the lock on success and must release it. Stop and the
        deadline are checked before every tick, during the tick via the
        bounded timeout, and once more after acquisition so Stop landing
        inside acquire() still wins.
        """
        while True:
            cancellation.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout_error(method)
            if lock.acquire(timeout=min(cancellation.POLL_INTERVAL, remaining)):
                try:
                    cancellation.check()
                except Exception:
                    with contextlib.suppress(Exception):
                        lock.release()
                    raise
                if deadline - time.monotonic() <= 0:
                    with contextlib.suppress(Exception):
                        lock.release()
                    raise self._timeout_error(method)
                return

    def _request(self, method: str, params: dict, timeout: float | None):
        # Verdict deadline governs gate wait, life-lock wait, stdin write,
        # and reply wait. Spawning is a bounded syscall checked before and
        # after; it never blocks on the pipe. Retire cleanup (page close,
        # tree terminate, pipe close, bounded joins) has its own bounded
        # contract and is not counted in the verdict deadline. The write runs
        # on the session writer thread, so Stop and the deadline interrupt
        # the caller even when the pipe is full.
        deadline = time.monotonic() + (timeout if timeout is not None else 300.0) + (
            WORKER_TIMEOUT_GRACE
        )
        self._acquire_with_deadline(self._request_lock, deadline, method)
        try:
            return self._request_locked(method, params, deadline)
        finally:
            with contextlib.suppress(Exception):
                self._request_lock.release()

    def _retire_stuck_slot_bounded(self, session: _WorkerSession) -> None:
        """Bounded cleanup for a slot wait that timed out or stopped.

        Best effort only and never masking the caller's verdict: acquire
        briefly, retire when the slot is still owned and still stuck, then
        release. A slot that just freed stays reusable.
        """
        acquired = False
        try:
            acquired = self._life_lock.acquire(timeout=5.0)
        except Exception:
            return
        if not acquired:
            return
        try:
            if self._session is not session:
                return
            with session.lock:
                stuck = (
                    session.pending is not None
                    or session.write_wire is not None
                    or bool(session.terminal_error)
                )
            if stuck:
                with contextlib.suppress(Exception):
                    self._retire_session_locked(session)
        finally:
            with contextlib.suppress(Exception):
                self._life_lock.release()

    def _request_locked(self, method: str, params: dict, deadline: float):
        # Serialize before touching the session: a serialization failure
        # must never occupy the single-flight slot. A sticky terminal_error
        # (e.g. a failed stdin write) retires via _ensure on re-entry, so a
        # cleared slot never wipes it: registration only resets the
        # per-request write_error, never the generation verdict.
        request_id = uuid.uuid4().hex
        wire = json.dumps(
            {"id": request_id, "method": method, "params": params},
            separators=(",", ":"),
        ) + "\n"
        pending: _PendingRequest | None = None
        session: _WorkerSession | None = None
        stuck_session: _WorkerSession | None = None
        while True:
            # Acquire, check, and decide while holding the lock.
            try:
                self._acquire_with_deadline(self._life_lock, deadline, method)
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                if stuck_session is not None:
                    self._retire_stuck_slot_bounded(stuck_session)
                raise
            except ProviderActionError:
                if stuck_session is not None:
                    self._retire_stuck_slot_bounded(stuck_session)
                raise
            try:
                session = self._ensure_live_session_locked()
                try:
                    cancellation.check()
                    if deadline - time.monotonic() <= 0:
                        raise self._timeout_error(method)
                except (cancellation.TaskCancelled, cancellation.DeadlineExceeded, ProviderActionError):
                    # A start that finishes after Stop/deadline must not
                    # publish an unused live worker for the next request.
                    with contextlib.suppress(Exception):
                        self._retire_session_locked(session)
                    raise
                if session.pending is not None:
                    raise RuntimeError("provider worker single-flight invariant violated")
                if session.write_wire is None:
                    if session.proc.poll() is not None or session.proc.stdin is None:
                        raise RuntimeError("provider worker is not running")
                    pending = _PendingRequest(request_id=request_id, method=method)
                    with session.lock:
                        session.pending = pending
                        session.write_wire = wire
                        session.write_error = ""
                        session.write_done.clear()
                        session.write_event.set()
                    break
                prev_write_done = session.write_done
                stuck_session = session
            finally:
                with contextlib.suppress(Exception):
                    self._life_lock.release()
            # Wait outside the lock, then re-enter a fresh round.
            try:
                cancellation.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(method)
                prev_write_done.wait(
                    timeout=min(cancellation.POLL_INTERVAL, remaining)
                )
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                self._retire_stuck_slot_bounded(stuck_session)
                raise
            except ProviderActionError:
                self._retire_stuck_slot_bounded(stuck_session)
                raise
        assert pending is not None and session is not None
        self._await_write(session, pending, deadline)
        return self._wait_for_response(session, pending, deadline)

    def _await_write(
        self,
        session: _WorkerSession,
        pending: _PendingRequest,
        deadline: float,
    ) -> None:
        """Wait for the session writer without ever blocking in write().

        Order is Stop, then the one-time verdict, then close: a reply in
        pending proves delivery even when write_done or a non-Stop close
        races behind it.
        """
        try:
            while True:
                cancellation.check()
                with session.lock:
                    verdict_done = pending.done.is_set()
                    verdict_response = pending.response
                    verdict_error = pending.error
                    closed = session.closed
                    write_done_flag = session.write_done.is_set()
                if verdict_done and (
                    verdict_response is not None or verdict_error
                ):
                    return
                if closed:
                    # Retire and close always mark the owned generation closed:
                    # no life-lock pointer check is needed on this hot path.
                    raise self._exited_error(session, pending.method)
                if write_done_flag:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(pending.method)
                if session.proc.poll() is not None and session.stdout_done.is_set():
                    # Reader drained with no verdict: the write can never
                    # produce a reply, so fail fast instead of waiting for
                    # the writer tick.
                    raise self._exited_error(session, pending.method)
                session.write_done.wait(
                    timeout=min(cancellation.POLL_INTERVAL, remaining)
                )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            with self._life_lock:
                if self._session is session:
                    self._retire_session_locked(session)
            raise
        except ProviderActionError:
            with self._life_lock:
                if self._session is session:
                    self._retire_session_locked(session)
            raise
        with session.lock:
            if pending.done.is_set() and pending.response is not None:
                return
            write_error = session.write_error
        if write_error:
            # The writer already cleared its slot; drop the orphaned pending
            # slot (no waiter exists yet) and retire when still current.
            with self._life_lock:
                if self._session is session:
                    self._retire_session_locked(session)
            with session.lock:
                if session.pending is pending:
                    session.pending = None
            raise RuntimeError(write_error)

    def _wait_for_response(
        self,
        session: _WorkerSession,
        pending: _PendingRequest,
        deadline: float,
    ):
        """Wait for the single verdict: Stop, then verdict, then close.

        Replacement is observed via our own closed flag, which retire and
        close always set. No life-lock pointer check runs on this hot path,
        so a close holding the life lock for cleanup never stretches the
        verdict wait.
        """
        drain_deadline: float | None = None
        try:
            while True:
                # Stop first: a flood of unrelated frames must never starve
                # cancellation. The post-wake check below covers Stop that
                # lands during the short wait itself.
                cancellation.check()
                with session.lock:
                    done = pending.done.is_set()
                    response = pending.response
                    error = pending.error
                    closed = session.closed
                    terminal_error = session.terminal_error
                    reader_drained = session.stdout_done.is_set()
                if done:
                    if response is not None:
                        if response.get("ok") is True:
                            return response.get("result")
                        failure = _failure_from_response(
                            self.provider_id, pending.method, response
                        )
                        self.last_failure = failure
                        raise ProviderActionError(failure)
                    if error or terminal_error:
                        # Protocol verdict wins over a racing close: it is
                        # the specific condemn cause, not a generic exit.
                        with self._life_lock:
                            if self._session is session:
                                self._retire_session_locked(session)
                        raise self._protocol_error(pending.method, error or terminal_error)
                    if closed:
                        # Woken by close/retire with no verdict: the
                        # generation is gone, never a protocol failure.
                        raise self._exited_error(session, pending.method)
                    with self._life_lock:
                        if self._session is session:
                            self._retire_session_locked(session)
                    raise self._protocol_error(pending.method, error or terminal_error)
                if closed:
                    raise self._exited_error(session, pending.method)
                if terminal_error:
                    # Framing already failed with no verdict for us: retire
                    # when still current and report the protocol error
                    # instead of waiting out the timeout.
                    with self._life_lock:
                        if self._session is session:
                            self._retire_session_locked(session)
                    raise self._protocol_error(pending.method, terminal_error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._life_lock:
                        if self._session is session:
                            self._retire_session_locked(session)
                    raise self._timeout_error(pending.method)
                if session.proc.poll() is not None:
                    if reader_drained:
                        # Reader already drained with no verdict: fail fast
                        # instead of holding the waiter for the full grace.
                        # The grace is only for a live reader that may still
                        # flush a buffered reply.
                        with self._life_lock:
                            if self._session is session:
                                self._retire_session_locked(session)
                        raise self._exited_error(session, pending.method)
                    # Exited but possibly with a buffered reply still in the
                    # pipe: let the reader drain briefly instead of failing
                    # a completed operation. The reader reports EOF verdicts
                    # itself; silence past the grace is the exit failure.
                    now = time.monotonic()
                    if drain_deadline is None:
                        drain_deadline = now + EXIT_DRAIN_GRACE
                    drain_left = drain_deadline - now
                    if drain_left <= 0:
                        with self._life_lock:
                            if self._session is session:
                                self._retire_session_locked(session)
                        raise self._exited_error(session, pending.method)
                    pending.done.wait(
                        timeout=min(cancellation.POLL_INTERVAL, remaining, drain_left)
                    )
                    cancellation.check()
                    continue
                drain_deadline = None
                # Short waits keep Stop responsive without pinning the run
                # until timeout after the user cancelled.
                pending.done.wait(timeout=min(cancellation.POLL_INTERVAL, remaining))
                cancellation.check()
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            # Stop retires only this generation: detach and tear it down
            # when still current, then let cancellation propagate.
            with self._life_lock:
                if self._session is session:
                    self._retire_session_locked(session)
            raise
        finally:
            with session.lock:
                if session.pending is pending:
                    session.pending = None

    def _timeout_error(self, method: str) -> ProviderActionError:
        failure = ProviderFailure(
            self.provider_id,
            method,
            "",
            "",
            "provider worker timed out",
            "",
            FAILURE_RESPONSE_MISSING,
        )
        self.last_failure = failure
        return ProviderActionError(failure)

    def _exited_error(
        self, session: _WorkerSession, method: str
    ) -> ProviderActionError:
        failure = ProviderFailure(
            self.provider_id,
            method,
            "",
            "",
            "provider worker exited" + self._worker_error_suffix(session),
            "",
            FAILURE_RESPONSE_MISSING,
        )
        self.last_failure = failure
        return ProviderActionError(failure)

    def _protocol_error(
        self, method: str, message: str
    ) -> ProviderActionError:
        failure = ProviderFailure(
            self.provider_id,
            method,
            "",
            "",
            message or "provider worker sent a malformed frame",
            "",
            FAILURE_RESPONSE_MISSING,
        )
        self.last_failure = failure
        return ProviderActionError(failure)

    def _retire_session_locked(
        self, session: _WorkerSession, *, retry_live_child: bool = False
    ) -> bool:
        """Retire this generation; publish an empty slot only after cleanup."""
        if self._session is not session:
            return False
        with session.lock:
            already_closed = session.closed
            session.closed = True
            pending = session.pending
            if pending is not None:
                pending.done.set()
            session.write_event.set()
        if not already_closed:
            threads_stopped = self._terminate_session(session)
        elif retry_live_child and session.proc.poll() is None:
            # Explicit close retries a live direct child with the same
            # bounded terminate-and-reap. Rechecks keep the fast-fail so a
            # short request deadline never waits; an exited child is never
            # signalled again (its PID may already be recycled).
            threads_stopped = self._terminate_session(session)
        else:
            threads_stopped = all(
                not thread.is_alive() for thread in self._session_threads(session)
            )
        # A stopped reader alone does not prove the worker released its
        # browser profile. The direct child must have exited as well.
        complete = threads_stopped and session.proc.poll() is not None
        if complete:
            self._session = None
        return complete

    @staticmethod
    def _session_threads(session: _WorkerSession) -> list[threading.Thread]:
        return [
            thread
            for thread in (session.reader, session.stderr_reader, session.writer)
            if thread is not None
        ]

    def _terminate_session(self, session: _WorkerSession) -> bool:
        """Tear down a closed generation: page, tree, job, threads, pipes.

        Page close runs before tree termination while the caller still holds
        `_life_lock`, so a replacement generation cannot reuse the browser
        profile early. Terminate first, join bounded, then close only stopped
        threads' pipes: closing a live reader's pipe would wait on its lock.
        Returns True when the generation's threads stopped within budget.
        """
        with session.lock:
            port = session.cdp_port
            target_id = session.target_id
            session.target_id = ""
        if port and target_id:
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/json/close/{quote(target_id, safe='')}",
                    timeout=2.0,
                ):
                    pass
            except Exception:
                pass
        proc = session.proc
        job = session.job
        joined = False
        try:
            cancellation.terminate_process_tree(proc, job)
        finally:
            if job is not None:
                with contextlib.suppress(Exception):
                    job.close()  # type: ignore[union-attr]
            joined = self._join_threads(self._session_threads(session))
            self._close_stopped_session_pipes(session)
        return joined

    @staticmethod
    def _close_all_pipes(proc: subprocess.Popen[str]) -> None:
        """Close pipes when no session thread owns them (spawn failure path)."""
        for stream in (
            getattr(proc, "stdin", None),
            getattr(proc, "stdout", None),
            getattr(proc, "stderr", None),
        ):
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass

    @staticmethod
    def _close_stopped_session_pipes(session: _WorkerSession) -> None:
        """Close only pipes whose owner thread already stopped.

        A live reader/writer owns its pipe; closing it would block on the
        thread's IO lock when another process still holds the write end.
        Skipped pipes stay abandoned (daemon threads) and the retire result
        still reports incomplete cleanup via ``False``.
        """
        proc = session.proc
        pairs = (
            (getattr(proc, "stdin", None), session.writer),
            (getattr(proc, "stdout", None), session.reader),
            (getattr(proc, "stderr", None), session.stderr_reader),
        )
        current = threading.current_thread()
        for stream, thread in pairs:
            if stream is None:
                continue
            if thread is not None and thread is not current and thread.is_alive():
                continue
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass

    @staticmethod
    def _join_threads(threads: list[threading.Thread]) -> bool:
        budget = max(0.0, float(READER_JOIN_TIMEOUT))
        deadline = time.monotonic() + budget
        for thread in threads:
            if thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            with contextlib.suppress(Exception):
                thread.join(timeout=max(0.0, remaining))
        return all(
            not thread.is_alive()
            for thread in threads
            if thread is not threading.current_thread()
        )


def _protocol_error(method: str, payload: dict) -> str | None:
    """Validate a matching-id reply shape. None means the frame is usable."""
    if not isinstance(payload.get("ok"), bool):
        return "provider worker sent a malformed frame"
    if payload.get("ok") is True:
        # A bare {"ok": true} would degrade to "" downstream: require
        # the string the contract promises instead of a silent success.
        if method == "send" and not isinstance(payload.get("result"), str):
            return "provider worker sent a malformed send reply"
        if method == "new_chat" and payload.get("result") is not None:
            return "provider worker sent a malformed new_chat reply"
    return None


def _failure_from_response(provider_id: str, method: str, response: dict) -> ProviderFailure:
    raw = response.get("failure")
    if isinstance(raw, dict):
        return ProviderFailure(
            str(raw.get("model") or provider_id),
            str(raw.get("action") or method),
            "",
            "",
            str(raw.get("message") or response.get("error") or "provider worker failed"),
            str(raw.get("time") or ""),
            str(raw.get("kind") or FAILURE_RESPONSE_MISSING),
            str(raw.get("stage") or ""),
            facts=raw.get("facts") if isinstance(raw.get("facts"), dict) else {},
        )
    return ProviderFailure(
        provider_id,
        method,
        "",
        "",
        str(response.get("error") or "provider worker failed"),
        "",
        FAILURE_RESPONSE_MISSING,
    )
