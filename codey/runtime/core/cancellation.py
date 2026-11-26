"""Task-local cooperative cancellation shared by blocking runtime paths."""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Protocol, SupportsIndex, SupportsInt, TypedDict, runtime_checkable

from codey.runtime.core.output_capture import (
    DRAIN_TIMEOUT_SECONDS,
    READ_CHUNK_BYTES,
    READER_CLEANUP_TIMEOUT_SECONDS,
    BoundedByteCapture,
)
from codey.runtime.core.process_io import ChildProcess, Process, ProcessIO, ProcessOptions, ReadPipe, start_terminal

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.2


class ProcessTreeOwner(Protocol):
    """Platform-specific owner for a spawned process tree."""

    def terminate(self) -> None: ...
    def close(self) -> None: ...


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class ProcessOutputObserver(Protocol):
    def offer(self, channel: str, chunk: bytes) -> None: ...
    def close(self) -> None: ...


class _ProcessGroupArgs(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


def _safe_capture_limit(value: object) -> int:
    from codey.runtime.core.output_capture import CAPTURE_LIMIT_BYTES as _default
    default_limit: int = _default
    if isinstance(value, bool):
        return default_limit
    if not isinstance(value, (str, bytes, bytearray, SupportsInt, SupportsIndex)):
        return default_limit
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default_limit
    return max(1, parsed)


def _safe_timeout(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        if not isinstance(value, (str, int, float)):
            return 0.0
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    import math as _math
    if not _math.isfinite(number) or number < 0:
        return 0.0
    return number
class _CancellationContext(threading.local):
    def __init__(self) -> None:
        self.event: threading.Event | None = None
        self.deadline: float | None = None


_context = _CancellationContext()


class _WindowsJob:
    """Keep the existing job attachment boundary, backed by typed native calls."""

    def __init__(self, proc: ChildProcess) -> None:
        from codey.runtime.core.windows_process import WindowsJob

        self._job = WindowsJob(proc.pid)

    def terminate(self) -> None:
        self._job.terminate()

    def close(self) -> None:
        self._job.close()


class TaskCancelled(RuntimeError):
    """Raised when the user stops the active task."""


class DeadlineExceeded(TimeoutError):
    """Raised when a bounded provider operation exhausts its total budget."""


class PipeDrainTimeout(RuntimeError):
    """Parent exited but output pipes never reached EOF (grandchild holds them)."""


class ProcessOutputReadError(RuntimeError):
    """A pipe reader failed before EOF; the captured output is incomplete."""


@dataclasses.dataclass
class _StreamPump:
    """A finished pump no longer accesses its stream or capture."""

    capture: BoundedByteCapture
    error: Exception | None = None
    finished: threading.Event = dataclasses.field(default_factory=threading.Event)
    observer: ProcessOutputObserver | None = None
    channel: str = "stdout"


@dataclasses.dataclass(frozen=True)
class CapturedProcess:
    """Bounded result of a spawned process (replaces CompletedProcess)."""

    args: str | Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


def current_event() -> threading.Event | None:
    return _context.event


def current_deadline() -> float | None:
    return _context.deadline


def set_event(event: threading.Event | None) -> threading.Event | None:
    previous = current_event()
    _context.event = event
    return previous


@contextmanager
def scope(event: threading.Event | None) -> Generator[None, None, None]:
    previous = set_event(event)
    try:
        yield
    finally:
        set_event(previous)


@contextmanager
def deadline_scope(deadline: float | None) -> Generator[None, None, None]:
    previous = current_deadline()
    active = deadline
    if previous is not None and (active is None or previous < active):
        active = previous
    _context.deadline = active
    try:
        yield
    finally:
        _context.deadline = previous


@contextmanager
def cleanup_scope() -> Generator[None, None, None]:
    """Allow bounded resource cleanup after its owning operation was cancelled."""
    event, deadline = _context.event, _context.deadline
    _context.event, _context.deadline = None, None
    try:
        yield
    finally:
        _context.event, _context.deadline = event, deadline


class _LinkedEvent(threading.Event):
    """A local stop request also observes its enclosing cancellation sources."""

    def __init__(self, parents: tuple[CancellationSignal, ...]) -> None:
        super().__init__()
        self._parents = parents

    def is_set(self) -> bool:
        return super().is_set() or any(parent.is_set() for parent in self._parents)

    def wait(self, timeout: float | None = None) -> bool:
        end = None if timeout is None else time.monotonic() + _safe_timeout(timeout)
        while not self.is_set():
            remaining = POLL_INTERVAL if end is None else min(POLL_INTERVAL, end - time.monotonic())
            if remaining <= 0:
                return self.is_set()
            super().wait(remaining)
        return True


@contextmanager
def linked_scope(
    event: CancellationSignal | None = None, *, deadline: float | None = None,
) -> Generator[threading.Event, None, None]:
    """Own local cancellation without replacing the enclosing stop or deadline.

    Blocking child threads receive the yielded event and current deadline through
    the ordinary scopes. The owner must join its children before leaving.
    """
    parents = tuple(parent for parent in (current_event(), event) if parent is not None)
    local = _LinkedEvent(parents)
    with scope(local), deadline_scope(deadline):
        try:
            yield local
        finally:
            local.set()


def check() -> None:
    event = current_event()
    if event is not None and event.is_set():
        raise TaskCancelled("task stopped")
    deadline = current_deadline()
    if deadline is not None and time.monotonic() >= deadline:
        raise DeadlineExceeded("provider operation timed out")


def wait(seconds: float) -> None:
    check()
    event = current_event()
    timeout = _safe_timeout(seconds)
    deadline = current_deadline()
    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
    if event is None:
        time.sleep(timeout)
    elif event.wait(timeout):
        raise TaskCancelled("task stopped")
    check()


def start_process(
    args: str | Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None = None,
    shell: bool = False,
    stdin: bool = False,
    hide_console: bool = False,
) -> tuple[subprocess.Popen[bytes], ProcessTreeOwner | None]:
    """Check cancellation once, then spawn. Callers holding a spawn gate must
    keep the gate across their final Stop check and this call so Stop cannot
    land between check and Popen."""
    check()
    # CREATE_SUSPENDED keeps Windows descendants inside their Job before execution.
    group_args: _ProcessGroupArgs = {"start_new_session": True}
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW if hide_console else subprocess.CREATE_NEW_PROCESS_GROUP
        group_args = {"creationflags": flags | 0x4}
    process_env = os.environ.copy() if env is None else dict(env)
    process_env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=process_env,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        shell=shell,
        **group_args,
    )
    try:
        job = attach_process_tree(proc)
    except BaseException:
        if proc.stdin is not None:
            proc.stdin.close()
        raise
    if os.name == "nt":
        try:
            check()
            _resume_windows_process(proc)
        except BaseException:
            terminate_process_tree(proc, job)
            if job is not None:
                job.close()
            _close_pipes(proc)
            if proc.stdin is not None:
                proc.stdin.close()
            raise
    return proc, job


def _resume_windows_process(proc: subprocess.Popen[bytes]) -> None:
    """Resume a suspended Windows child only after Job attachment succeeds."""
    from codey.runtime.core.windows_process import resume_process

    resume_process(proc.pid)


def _pump_stream(stream: ReadPipe, state: _StreamPump) -> None:
    """Drain one pipe into its bounded capture; a read failure is sticky."""
    try:
        read_chunk = stream.read1 if isinstance(stream, _BufferedReadPipe) else stream.read
        while True:
            chunk = read_chunk(READ_CHUNK_BYTES)
            if not chunk:
                return
            state.capture.feed(chunk)
            if state.observer is not None:
                with suppress(Exception):
                    state.observer.offer(state.channel, chunk)
                    # Observation is auxiliary; a failed observer never
                    # changes the execution result or stops pipe draining.
    except Exception as exc:
        state.error = exc
    finally:
        state.finished.set()


def _wait_readers(states: list[_StreamPump], *, timeout: float) -> None:
    budget = max(0.0, float(timeout))
    deadline = time.monotonic() + budget
    for state in states:
        remaining = deadline - time.monotonic()
        state.finished.wait(timeout=max(0.0, remaining))


def _close_pipes(proc: Process | subprocess.Popen[str]) -> None:
    """Close owned pipes when no reader thread exists (spawn failure path)."""
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _close_owned_pipes(
    proc: Process,
    owned: list[tuple[object, _StreamPump]],
) -> int:
    """Close only pipes whose pump finished; never block on live reads.

    Returns the number of abandoned pipes (live readers). A live reader
    owns its pipe: another process may still hold the write end, so
    ``BufferedReader.close()`` would wait on the reader's lock. Skipping
    it bounds the caller; the daemon reader is abandoned and the caller
    must keep the incomplete-cleanup diagnosis instead of claiming full
    recovery. Repeated external holders therefore accumulate daemon
    readers by design; normal subtrees die with the process group and
    leave zero abandoned.
    """
    by_stream = {id(stream): state for stream, state in owned}
    abandoned = 0
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        state = by_stream.get(id(stream))
        if state is not None and not state.finished.is_set():
            abandoned += 1
            continue
        with suppress(Exception):
            stream.close()
    if abandoned:
        logger.warning(
            "process pipes incomplete: %d reader(s) still hold stdout/stderr; "
            "args=%r pid=%r",
            abandoned,
            proc.args,
            proc.pid,
        )
    return abandoned


def wait_process(
    proc: Process,
    job: ProcessTreeOwner | None,
    args: str | Sequence[str],
    timeout: float,
    *,
    capture_limit_bytes: int,
    observer: ProcessOutputObserver | None = None,
) -> CapturedProcess:
    """Wait with bounded per-stream capture; never buffers output unbounded.

    This function owns the whole lifecycle: readers, the process tree, the
    pipes, and the Job handle are all cleaned in one ``finally`` unless the
    run completed. Failure branches only raise; they never clean up
    themselves, so cleanup can neither be skipped nor mask the error.

    Cleanup terminates the owned tree, waits for pump completion within one
    shared budget, then closes finished pumps' pipes. Native thread teardown
    is not evidence of EOF and does not own the output drain deadline.
    """
    readers: list[_StreamPump] = []
    owned: list[tuple[object, _StreamPump]] = []
    completed = False
    try:
        limit = _safe_capture_limit(capture_limit_bytes)
        head = limit // 4
        tail = limit - head
        stdout_state = _StreamPump(BoundedByteCapture(head_limit=head, tail_limit=tail), observer=observer, channel="stdout")
        stderr_state = _StreamPump(BoundedByteCapture(head_limit=head, tail_limit=tail), observer=observer, channel="stderr")
        streams = (
            (proc.stdout, stdout_state),
            (proc.stderr, stderr_state),
        )
        for stream, state in streams:
            if stream is None:
                continue
            thread = threading.Thread(
                target=_pump_stream, args=(stream, state), daemon=True
            )
            thread.start()
            readers.append(state)
            owned.append((stream, state))
        deadline = time.monotonic() + _safe_timeout(timeout)
        while True:
            for state in (stdout_state, stderr_state):
                if state.error is not None:
                    raise ProcessOutputReadError(
                        "failed reading command output "
                        f"({state.error}); output is incomplete"
                    )
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                returncode = proc.wait(timeout=min(POLL_INTERVAL, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        _wait_readers(readers, timeout=DRAIN_TIMEOUT_SECONDS)
        if any(not state.finished.is_set() for state in readers):
            raise PipeDrainTimeout(
                "output pipe drain timed out: parent exited but a child "
                "still holds stdout/stderr; output is incomplete"
            )
        for state in (stdout_state, stderr_state):
            if state.error is not None:
                raise ProcessOutputReadError(
                    "failed reading command output "
                    f"({state.error}); output is incomplete"
                )
        stdout_done = stdout_state.capture.finish()
        stderr_done = stderr_state.capture.finish()
        if observer is not None:
            observer.close()
        result = CapturedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout_done.text,
            stderr=stderr_done.text,
            stdout_bytes=stdout_done.total_bytes,
            stderr_bytes=stderr_done.total_bytes,
            stdout_truncated=stdout_done.truncated,
            stderr_truncated=stderr_done.truncated,
        )
        completed = True
        return result
    finally:
        if not completed:
            _terminate_process_tree(proc, job)
            _wait_readers(readers, timeout=READER_CLEANUP_TIMEOUT_SECONDS)
        if observer is not None:
            with suppress(Exception):
                observer.close()
        _close_owned_pipes(proc, owned)
        if job is not None:
            with suppress(Exception):
                job.close()


def run_process(
    args: str | Sequence[str],
    *,
    cwd: str | Path,
    timeout: float,
    env: dict[str, str] | None = None,
    shell: bool = False,
    capture_limit_bytes: int,
    observer: ProcessOutputObserver | None = None,
    options: ProcessOptions | None = None,
    on_started: Callable[[ProcessIO], Callable[[], None]] | None = None,
) -> CapturedProcess:
    """One execution/collection owner for ordinary pipes and terminal backends."""
    check()
    options = options if options is not None else ProcessOptions()
    endpoint: ProcessIO | None = None
    unregister: Callable[[], None] | None = None
    proc: Process
    job: ProcessTreeOwner | None
    if options.tty:
        if shell:
            raise ValueError("terminal arguments must contain the explicit shell executable")
        process_env = os.environ.copy() if env is None else dict(env)
        process_env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        proc, job, endpoint = start_terminal(args, cwd=cwd, env=process_env, options=options)
    else:
        pipe, job = start_process(args, cwd=cwd, env=env, shell=shell, stdin=options.stdin,
                                  hide_console=options.hide_console)
        proc = pipe
        try:
            endpoint = ProcessIO(proc, pipe.stdin)
        except BaseException:
            _terminate_process_tree(proc, job)
            _close_pipes(proc)
            if pipe.stdin is not None:
                pipe.stdin.close()
            if job is not None:
                job.close()
            raise
    try:
        check()
        if on_started is not None:
            unregister = on_started(endpoint)
    except BaseException:
        endpoint.close()
        _terminate_process_tree(proc, job)
        _close_pipes(proc)
        if job is not None:
            job.close()
        _close_terminal_process(proc)
        raise
    try:
        return wait_process(proc, job, args, timeout, capture_limit_bytes=capture_limit_bytes, observer=observer)
    finally:
        if unregister is not None:
            unregister()
        endpoint.close()
        _close_terminal_process(proc)


def _close_terminal_process(proc: Process) -> None:
    from codey.runtime.core.windows_process import ConPTYProcess

    if isinstance(proc, ConPTYProcess):
        proc.close()


def attach_process_tree(proc: Process | subprocess.Popen[str]) -> ProcessTreeOwner | None:
    """Attach a process to the platform process-tree owner, when available."""
    if os.name != "nt":
        return None
    try:
        return _WindowsJob(proc)
    except Exception:
        with suppress(Exception):
            proc.kill()
        with suppress(Exception):
            proc.wait(timeout=2)
        _close_pipes(proc)
        raise


def terminate_process_tree(
    proc: ChildProcess,
    job: ProcessTreeOwner | None = None,
) -> None:
    """Terminate a process tree started in its own process group.

    Contract: ``proc`` comes from ``start_process()`` (POSIX
    ``start_new_session=True``, Windows Job Object) so ``proc.pid`` is the
    group leader. Arbitrary external ``Popen`` objects need their own
    direct-child termination path and must not share this helper.
    """
    _terminate_process_tree(proc, job)


def terminate_direct_child(proc: ChildProcess) -> None:
    """Terminate one direct child without group signalling."""
    with suppress(Exception):
        proc.terminate()
    with suppress(Exception):
        proc.wait(timeout=1)
    with suppress(Exception):
        if proc.poll() is None:
            proc.kill()
    with suppress(Exception):
        proc.wait(timeout=1)


def _process_group_exists(pgid: int) -> bool:
    """Check a process group without touching its (possibly reaped) leader."""
    try:
        _kill_process_group(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _terminate_process_tree(
    proc: ChildProcess,
    job: ProcessTreeOwner | None = None,
) -> None:
    if os.name == "nt":
        if job is not None:
            with suppress(Exception):
                job.terminate()
        with suppress(Exception):
            proc.kill()
        try:
            proc.wait(timeout=1)
        except Exception:
            with suppress(Exception):
                proc.kill()
            with suppress(Exception):
                proc.wait(timeout=1)
    else:
        # Processes from start_process() run in a new session, so the known
        # group id is proc.pid: no getpgid() on a possibly reaped parent.
        pgid = proc.pid
        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
            # Not a real group leader (closed proc or non-process double):
            # fall back to direct-child termination instead of signalling
            # a meaningless group id.
            terminate_direct_child(proc)
            return
        try:
            _kill_process_group(pgid, signal.SIGTERM)
        except ProcessLookupError:
            with suppress(Exception):
                proc.terminate()
        except OSError:
            with suppress(Exception):
                proc.terminate()
        with suppress(Exception):
            proc.wait(timeout=1)
        # proc.wait() only reaps the direct child; grandchildren holding
        # pipes are judged by group existence, never by the parent's wait.
        if _process_group_exists(pgid):
            with suppress(Exception):
                _kill_process_group(pgid, 9)
            with suppress(Exception):
                proc.wait(timeout=1)


@runtime_checkable
class _BufferedReadPipe(ReadPipe, Protocol):
    def read1(self, size: int = -1, /) -> bytes: ...


if sys.platform != "win32":
    def _kill_process_group(pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)
else:
    def _kill_process_group(pgid: int, sig: int) -> None:
        raise ProcessLookupError("POSIX process groups are unavailable on Windows")
