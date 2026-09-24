"""Parent-side JSON-line Provider worker wrapper."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
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
RESPONSE_QUEUE_MAXSIZE = 512
# True read bounds: stdout frames with a per-line cap and stderr tails in
# fixed-size chunks, so one huge child line never enters memory whole.
WORKER_LINE_MAX_CHARS = 256 * 1024
WORKER_STDERR_CHUNK_CHARS = 16 * 1024
WORKER_STDERR_TAIL_CHUNKS = 4


@dataclass
class WorkerChatProvider:
    provider_id: str
    override: AdapterOverride
    port: int = DEFAULT_PORT + 100
    state_home: Path = DEFAULT_STATE_HOME

    def __post_init__(self) -> None:
        self.name = f"{self.provider_id} worker"
        self.last_failure: ProviderFailure | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._job = None
        self._responses: queue.Queue[dict] = queue.Queue(maxsize=RESPONSE_QUEUE_MAXSIZE)
        self._response_lock = threading.Lock()
        self._dropped_responses = 0
        # Split gates: _conn_lock guards proc lifecycle only; _request_lock
        # keeps stdin single-flight. Waiting for a response holds neither the
        # conn lock (so close() never queues behind a long send) nor blocks
        # lifecycle: close() terminates under _conn_lock and the waiter
        # observes proc death via its local handle.
        self._conn_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._cdp_port: int = 0
        self._target_id: str = ""
        # Reader verdict for the current generation only. Requests are
        # single-flight, so one slot suffices: a stale reader can neither
        # fail nor misroute a replacement process (see _condemn_reader).
        self._reader_error: str = ""
        with self._conn_lock:
            self._start_conn_locked()

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
        # Shutdown must never resurrect the child and must never queue behind
        # a long send: terminate under the conn lock only, without taking the
        # request gate. An in-flight _request observes proc death via its
        # local handle and fails fast instead of hanging the close.
        with self._conn_lock:
            self._terminate_conn_locked()

    def _start_conn_locked(self) -> None:
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
        self._proc = subprocess.Popen(
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
            self._job = cancellation.attach_process_tree(self._proc)
        except Exception:
            proc = self._proc
            self._proc = None
            self._job = None
            if proc is not None:
                with contextlib.suppress(Exception):
                    cancellation.terminate_process_tree(proc, None)
            raise
        # Reader threads capture their own generation explicitly: after a
        # restart they must never guess ownership from self._proc, and the
        # stderr tail is per-generation so old diagnostics cannot leak into
        # a replacement's record.
        proc = self._proc
        tail: deque[str] = deque(maxlen=WORKER_STDERR_TAIL_CHUNKS)
        self._stderr_tail = tail
        self._reader_error = ""
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, args=(proc, tail), daemon=True,
        )
        self._stderr_reader.start()
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader.start()

    def _start(self) -> None:
        """Public restart entry (tests patch this)."""
        with self._conn_lock:
            self._start_conn_locked()

    def _stderr_loop(self, proc, tail: deque[str]) -> None:
        # Startup diagnostics only, writing this generation's tail:
        # readline with a chunk cap keeps one huge line from ever entering
        # memory whole, while a short newline-terminated diagnostic lands
        # promptly instead of waiting for a full chunk or EOF. Any drain
        # failure must never surface.
        try:
            stderr = proc.stderr if proc is not None else None
            if stderr is None:
                return
            while True:
                chunk = stderr.readline(WORKER_STDERR_CHUNK_CHARS)
                if not chunk:
                    return
                tail.append(chunk)
        except Exception:
            return

    def _worker_error_suffix(self) -> str:
        tail = " | ".join(self._stderr_tail).strip()
        return f": {tail[-400:]}" if tail else ""

    def _read_loop(self, proc) -> None:
        stdout = proc.stdout if proc is not None else None
        if stdout is None:
            self._condemn_reader(proc, "provider worker stdout is unavailable")
            return
        while True:
            try:
                line = stdout.readline(WORKER_LINE_MAX_CHARS + 1)
            except (OSError, ValueError) as exc:
                self._condemn_reader(
                    proc, f"provider worker stdout unreadable: {exc}",
                )
                return
            if not line:
                # EOF on a live process means its framing died with no
                # verdict; a normally exited child is handled by the
                # proc-exit path in the waiter instead.
                if proc.poll() is None:
                    self._condemn_reader(
                        proc, "provider worker stdout closed unexpectedly",
                    )
                return
            if not line.endswith("\n") and len(line) > WORKER_LINE_MAX_CHARS:
                # Protocol failure of this worker, not a framable reply: a
                # chunk of MAX+1 chars without a newline means the line is
                # longer than the cap (a line of exactly MAX chars plus its
                # newline still fits and parses below). No draining, so the
                # next frame is never consumed as this one's tail.
                self._condemn_reader(
                    proc,
                    f"provider worker output exceeded {WORKER_LINE_MAX_CHARS} chars",
                )
                return
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                if payload.get("event") == "page":
                    self._record_worker_page(proc, payload)
                    continue
                self._offer_response_for(proc, payload)

    def _condemn_reader(self, proc, reason: str) -> None:
        """Record one generation's reader verdict, ignoring stale readers.

        Only the current generation may hold an error: a late verdict from
        a detached reader is dropped, so it can neither fail a waiter nor
        misroute a replacement process. The first verdict wins.
        """
        with self._conn_lock:
            if self._proc is not proc:
                return
            if not self._reader_error:
                self._reader_error = reason

    def _offer_response(self, payload: dict) -> None:
        """Bounded offer into _responses: newest wins, oldest drop is counted.

        A restart can leave the old reader briefly alive next to the new one,
        so the full/get/put sequence holds the response lock: without it two
        readers could both evict (double drop count) or both put (one newest
        lost). Request ids still filter stale responses either way.
        """
        with self._response_lock:
            if not self._responses.full():
                try:
                    self._responses.put_nowait(payload)
                except queue.Full:
                    return
                return
            try:
                self._responses.get_nowait()
            except queue.Empty:
                return
            self._dropped_responses += 1
            try:
                self._responses.put_nowait(payload)
            except queue.Full:
                return

    def _offer_response_for(self, proc, payload: dict) -> None:
        """Offer a frame only when its reader is still the current generation."""
        with self._conn_lock:
            if self._proc is not proc:
                return
        self._offer_response(payload)

    def _record_worker_page(self, proc, payload: dict) -> None:
        """Apply a page event only from the current generation's reader."""
        with self._conn_lock:
            if self._proc is not proc:
                return
            try:
                self._cdp_port = max(0, int(payload.get("port") or 0))
            except (TypeError, ValueError):
                self._cdp_port = 0
            self._target_id = str(payload.get("target_id") or "")

    def _drain_responses(self) -> None:
        with self._response_lock:
            while True:
                try:
                    self._responses.get_nowait()
                except queue.Empty:
                    return

    def _ensure_running_conn_locked(self) -> subprocess.Popen[str]:
        """Restart a dead worker. Caller must hold the conn lock.

        A live process whose reader already condemned it is equally
        unusable: its replies can never arrive, so restart instead of
        reusing it.
        """
        proc = self._proc
        if (
            proc is not None
            and proc.poll() is None
            and proc.stdin is not None
            and not self._reader_error
        ):
            return proc
        self._drain_responses()
        self._terminate_conn_locked()
        # Via the patchable _start() entry (conn lock is reentrant).
        self._start()
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("provider worker is not running")
        return proc

    def _request(
        self,
        method: str,
        params: dict,
        timeout: float | None,
        *,
        restart: bool = True,
        grace: bool = True,
    ):
        # Single-flight stdin via the request gate; the conn lock covers
        # proc selection only. Both the write below and the wait loop run
        # lock-free, so close()/Stop never queues behind a blocked stdin or
        # a long send: termination observes the request's local proc handle.
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
        with self._conn_lock:
            if restart:
                proc = self._ensure_running_conn_locked()
            else:
                proc = self._proc
                if proc is None or proc.poll() is not None or proc.stdin is None:
                    raise RuntimeError("provider worker is not running")
            request_id = uuid.uuid4().hex
            wire = json.dumps(
                {"id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            ) + "\n"
        try:
            proc.stdin.write(wire)
            proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as exc:
            with self._conn_lock:
                # Only reap the process this request actually used: a
                # concurrent close/restart may already have replaced it.
                if self._proc is proc:
                    self._terminate_conn_locked()
                    self._drain_responses()
            raise RuntimeError("provider worker stdin is unavailable") from exc
        return self._wait_for_response(proc, method, request_id, timeout, grace=grace)

    def _wait_for_response(
        self,
        proc: subprocess.Popen[str],
        method: str,
        request_id: str,
        timeout: float | None,
        *,
        grace: bool,
    ):
        deadline = time.monotonic() + (timeout if timeout is not None else 300.0) + (
            WORKER_TIMEOUT_GRACE if grace else 0.0
        )
        while True:
            with self._conn_lock:
                replaced = self._proc is not proc
                reader_error = "" if replaced else self._reader_error
            if replaced:
                # Our generation is gone (closed or restarted): end the wait
                # now instead of polling a dead handle until timeout. Never
                # terminate here: the current process belongs to someone else.
                self._drain_responses()
                failure = ProviderFailure(
                    self.provider_id,
                    method,
                    "",
                    "",
                    "provider worker exited" + self._worker_error_suffix(),
                    "",
                    FAILURE_RESPONSE_MISSING,
                )
                self.last_failure = failure
                raise ProviderActionError(failure)
            if reader_error:
                # This worker's framing already failed: terminate it if it
                # is still current and report the protocol error instead of
                # waiting out the timeout for replies that cannot arrive.
                with self._conn_lock:
                    if self._proc is proc:
                        self._terminate_conn_locked()
                self._drain_responses()
                failure = ProviderFailure(
                    self.provider_id,
                    method,
                    "",
                    "",
                    reader_error,
                    "",
                    FAILURE_RESPONSE_MISSING,
                )
                self.last_failure = failure
                raise ProviderActionError(failure)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate()
                self._drain_responses()
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
            if proc.poll() is not None:
                self._terminate()
                self._drain_responses()
                failure = ProviderFailure(
                    self.provider_id,
                    method,
                    "",
                    "",
                    "provider worker exited" + self._worker_error_suffix(),
                    "",
                    FAILURE_RESPONSE_MISSING,
                )
                self.last_failure = failure
                raise ProviderActionError(failure)
            # Small-step polling keeps Stop responsive: waiting on the
            # full remaining budget would pin the run until timeout even
            # after the user cancelled. Stale ids from a previous life are
            # skipped: each request filters by its own id (pending-by-id).
            try:
                response = self._responses.get(
                    timeout=min(cancellation.POLL_INTERVAL, remaining)
                )
            except queue.Empty:
                cancellation.check()
                continue
            if response.get("id") != request_id:
                continue
            if response.get("ok") is True:
                return response.get("result")
            failure = _failure_from_response(self.provider_id, method, response)
            self.last_failure = failure
            raise ProviderActionError(failure)

    def _terminate_conn_locked(self) -> None:
        """Terminate the child. Caller must hold the conn lock."""
        proc = self._proc
        self._proc = None
        job = self._job
        self._job = None
        if proc is None:
            return
        try:
            # Detaching clears the generation's verdict with it: nothing of
            # the old reader survives into the replacement's record.
            self._reader_error = ""
            self._close_worker_page()
            cancellation.terminate_process_tree(proc, job)
        finally:
            if job is not None:
                with contextlib.suppress(Exception):
                    job.close()

    def _terminate(self) -> None:
        with self._conn_lock:
            self._terminate_conn_locked()

    def _close_worker_page(self) -> None:
        port = self._cdp_port
        target_id = self._target_id
        self._target_id = ""
        if not port or not target_id:
            return
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/json/close/{quote(target_id, safe='')}",
                timeout=2.0,
            ):
                pass
        except Exception:
            pass


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
