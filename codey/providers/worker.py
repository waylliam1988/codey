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


@dataclass
class _PendingRequest:
    """One in-flight request slot owned by its waiter thread."""

    request_id: str
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None


@dataclass
class _WorkerSession:
    """One worker generation: proc plus everything that belongs to it.

    Readers only ever touch their own session object, so a late thread from
    a retired generation cannot fail, misroute, or pollute its replacement.
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


@dataclass
class WorkerChatProvider:
    provider_id: str
    override: AdapterOverride
    port: int = DEFAULT_PORT + 100
    state_home: Path = DEFAULT_STATE_HOME

    def __post_init__(self) -> None:
        self.name = f"{self.provider_id} worker"
        self.last_failure: ProviderFailure | None = None
        # _life_lock serializes session detach/terminate/start so a new
        # generation never reuses the browser profile while the old one is
        # still being torn down. _request_lock keeps stdin single-flight.
        # Waiting holds neither lock's critical path: close() detaches under
        # _life_lock and the waiter observes its local session handle.
        self._life_lock = threading.RLock()
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

    def close(self) -> None:
        # Shutdown never resurrects and never queues behind a long send:
        # detach under the life lock, wake the waiter, then tear down the
        # detached generation while still holding the lock so the next start
        # cannot reuse the profile early.
        with self._life_lock:
            session = self._session
            if session is None:
                return
            self._session = None
            with session.lock:
                session.closed = True
                pending = session.pending
                if pending is not None:
                    pending.done.set()
            self._terminate_session(session)

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
            self._close_pipes(proc)
            raise
        session = _WorkerSession(proc=proc, job=job)
        stderr_reader = threading.Thread(
            target=self._stderr_loop, args=(session,), daemon=True,
        )
        reader = threading.Thread(target=self._read_loop, args=(session,), daemon=True)
        session.stderr_reader = stderr_reader
        session.reader = reader
        started: list[threading.Thread] = []
        try:
            stderr_reader.start()
            started.append(stderr_reader)
            reader.start()
            started.append(reader)
        except Exception:
            with session.lock:
                session.closed = True
            with contextlib.suppress(Exception):
                cancellation.terminate_process_tree(proc, job)
            if job is not None:
                with contextlib.suppress(Exception):
                    job.close()  # type: ignore[union-attr]
            self._join_threads(started)
            self._close_pipes(proc)
            raise
        # Published only after both readers run: construction either hands
        # over a fully owned generation or cleans everything itself.
        self._session = session

    def _stderr_loop(self, session: _WorkerSession) -> None:
        # Startup diagnostics only, writing this generation's tail:
        # readline with a chunk cap keeps one huge line from ever entering
        # memory whole, while a short newline-terminated diagnostic lands
        # promptly instead of waiting for a full chunk or EOF. Any drain
        # failure must never surface.
        try:
            stderr = session.proc.stderr
            if stderr is None:
                return
            while True:
                chunk = stderr.readline(WORKER_STDERR_CHUNK_CHARS)
                if not chunk:
                    return
                session.stderr_tail.append(chunk)
        except Exception:
            return

    def _worker_error_suffix(self, session: _WorkerSession) -> str:
        tail = " | ".join(session.stderr_tail).strip()
        return f": {tail[-400:]}" if tail else ""

    def _read_loop(self, session: _WorkerSession) -> None:
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
                # verdict; a normally exited child is handled by the
                # proc-exit path in the waiter instead.
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
            if payload.get("event") == "page":
                self._deliver_page(session, payload)
                continue
            self._deliver_response(session, payload)

    def _fail_session(self, session: _WorkerSession, reason: str) -> None:
        """Record one generation's verdict and wake its waiter, first wins."""
        with session.lock:
            if session.closed:
                return
            if not session.terminal_error:
                session.terminal_error = reason
            pending = session.pending
            if pending is not None:
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
                session.terminal_error = session.terminal_error or (
                    "provider worker sent an unexpected reply"
                )
                return
            if payload.get("id") != pending.request_id:
                if not session.terminal_error:
                    session.terminal_error = (
                        "provider worker sent a reply for an unknown request"
                    )
                pending.done.set()
                return
            pending.response = payload
            pending.done.set()

    def _ensure_live_session_locked(self) -> subprocess.Popen[str]:
        """Return a usable proc, restarting a dead/condemned generation."""
        session = self._session
        if (
            session is not None
            and not session.closed
            and not session.terminal_error
            and session.proc.poll() is None
            and session.proc.stdin is not None
        ):
            return session.proc
        if session is not None:
            self._retire_session_locked(session)
        self._start_session_locked()
        session = self._session
        if session is None or session.proc.stdin is None:
            raise RuntimeError("provider worker is not running")
        return session.proc

    def _request(
        self,
        method: str,
        params: dict,
        timeout: float | None,
        *,
        restart: bool = True,
        grace: bool = True,
    ):
        # Single-flight stdin via the request gate; the life lock covers
        # session selection and pending registration only. The write and the
        # wait run lock-free, so close()/Stop never queues behind them.
        with self._request_lock:
            return self._request_locked(method, params, timeout, restart=restart, grace=grace)

    def _request_locked(
        self,
        method: str,
        params: dict,
        timeout: float | None,
        *,
        restart: bool = True,
        grace: bool = True,
    ):
        with self._life_lock:
            if restart:
                self._ensure_live_session_locked()
            session = self._session
            if (
                session is None
                or session.proc.poll() is not None
                or session.proc.stdin is None
            ):
                raise RuntimeError("provider worker is not running")
            # A verdict that lands between ensure and registration is not a
            # "not running" condition: the waiter below reports it as an
            # explicit protocol/close failure for this request.
            if session.pending is not None:
                # Defensive: single-flight was violated (or a prior waiter
                # leaked its slot). Retire instead of multiplexing stdin.
                self._retire_session_locked(session)
                self._start_session_locked()
                session = self._session
                if session is None or session.proc.stdin is None:
                    raise RuntimeError("provider worker is not running")
            request_id = uuid.uuid4().hex
            pending = _PendingRequest(request_id=request_id)
            proc = session.proc
            with session.lock:
                session.pending = pending
            wire = json.dumps(
                {"id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            ) + "\n"
        try:
            proc.stdin.write(wire)  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError, AttributeError) as exc:
            with self._life_lock:
                # Only reap the generation this request used: a concurrent
                # close/restart may already have replaced it.
                if self._session is session:
                    self._retire_session_locked(session)
                with session.lock:
                    if session.pending is pending:
                        session.pending = None
            raise RuntimeError("provider worker stdin is unavailable") from exc
        return self._wait_for_response(session, pending, method, timeout, grace=grace)

    def _wait_for_response(
        self,
        session: _WorkerSession,
        pending: _PendingRequest,
        method: str,
        timeout: float | None,
        *,
        grace: bool,
    ):
        deadline = time.monotonic() + (timeout if timeout is not None else 300.0) + (
            WORKER_TIMEOUT_GRACE if grace else 0.0
        )
        try:
            while True:
                # Stop first: a flood of wrong-id frames must never starve
                # cancellation. The post-wake check below covers Stop that
                # lands during the short wait itself.
                cancellation.check()
                with self._life_lock:
                    replaced = self._session is not session
                if replaced:
                    # Our generation is gone (closed or restarted): end now
                    # instead of polling a dead handle until timeout. Never
                    # terminate here: the current process belongs to someone
                    # else.
                    with session.lock:
                        if session.pending is pending:
                            session.pending = None
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
                    raise ProviderActionError(failure)
                with session.lock:
                    terminal_error = session.terminal_error
                    closed = session.closed
                if closed:
                    with session.lock:
                        if session.pending is pending:
                            session.pending = None
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
                    raise ProviderActionError(failure)
                if terminal_error:
                    # This worker's framing already failed: retire it if it
                    # is still current and report the protocol error instead
                    # of waiting out the timeout for replies that cannot
                    # arrive.
                    with self._life_lock:
                        if self._session is session:
                            self._retire_session_locked(session)
                    with session.lock:
                        if session.pending is pending:
                            session.pending = None
                    failure = ProviderFailure(
                        self.provider_id,
                        method,
                        "",
                        "",
                        terminal_error,
                        "",
                        FAILURE_RESPONSE_MISSING,
                    )
                    self.last_failure = failure
                    raise ProviderActionError(failure)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._life_lock:
                        if self._session is session:
                            self._retire_session_locked(session)
                    with session.lock:
                        if session.pending is pending:
                            session.pending = None
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
                    raise ProviderActionError(failure)
                if session.proc.poll() is not None:
                    with self._life_lock:
                        if self._session is session:
                            self._retire_session_locked(session)
                    with session.lock:
                        if session.pending is pending:
                            session.pending = None
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
                    raise ProviderActionError(failure)
                # Short waits keep Stop responsive without pinning the run
                # until timeout after the user cancelled.
                pending.done.wait(timeout=min(cancellation.POLL_INTERVAL, remaining))
                cancellation.check()
                with session.lock:
                    response = pending.response
                    if response is not None:
                        if session.pending is pending:
                            session.pending = None
                    else:
                        response = None
                if response is None:
                    continue
                if response.get("ok") is True:
                    return response.get("result")
                failure = _failure_from_response(self.provider_id, method, response)
                self.last_failure = failure
                raise ProviderActionError(failure)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            # Stop retires only this generation: wake, detach, and tear it
            # down when still current, then let cancellation propagate.
            with self._life_lock:
                if self._session is session:
                    self._retire_session_locked(session)
            with session.lock:
                if session.pending is pending:
                    session.pending = None
            raise

    def _retire_session_locked(self, session: _WorkerSession) -> bool:
        """Detach and tear down the current generation. Holds _life_lock."""
        if self._session is not session:
            return False
        self._session = None
        with session.lock:
            session.closed = True
            pending = session.pending
            if pending is not None:
                pending.done.set()
        self._terminate_session(session)
        return True

    def _terminate_session(self, session: _WorkerSession) -> None:
        """Tear down a detached generation: page, tree, job, pipes, threads."""
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
        try:
            cancellation.terminate_process_tree(proc, job)
        finally:
            if job is not None:
                with contextlib.suppress(Exception):
                    job.close()  # type: ignore[union-attr]
            self._close_pipes(proc)
            self._join_threads([
                thread
                for thread in (session.reader, session.stderr_reader)
                if thread is not None
            ])

    def _terminate(self) -> None:
        with self._life_lock:
            session = self._session
            if session is None:
                return
            self._retire_session_locked(session)

    @staticmethod
    def _close_pipes(proc: subprocess.Popen[str]) -> None:
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
    def _join_threads(threads: list[threading.Thread]) -> None:
        budget = max(0.0, float(READER_JOIN_TIMEOUT))
        deadline = time.monotonic() + budget
        for thread in threads:
            if thread is threading.current_thread():
                continue
            remaining = deadline - time.monotonic()
            with contextlib.suppress(Exception):
                thread.join(timeout=max(0.0, remaining))


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
