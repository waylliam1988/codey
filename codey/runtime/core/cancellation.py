"""Task-local cooperative cancellation shared by blocking runtime paths."""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO

from codey.runtime.core.output_capture import (
    DRAIN_TIMEOUT_SECONDS,
    READ_CHUNK_BYTES,
    READER_JOIN_TIMEOUT_SECONDS,
    BoundedByteCapture,
)

POLL_INTERVAL = 0.2
_context = threading.local()


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class _WindowsJob:
    """Own a Windows process tree and terminate it when the handle closes."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(
                handle,
                wintypes.HANDLE(proc._handle),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.close()
            raise

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            # Closing a KILL_ON_JOB_CLOSE job is the reliable fallback.
            self.close()

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


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
    """Per-stream reader state: capture plus a sticky read error."""

    capture: BoundedByteCapture
    error: Exception | None = None


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
    return getattr(_context, "event", None)


def current_deadline() -> float | None:
    return getattr(_context, "deadline", None)


def set_event(event: threading.Event | None) -> threading.Event | None:
    previous = current_event()
    _context.event = event
    return previous


@contextmanager
def scope(event: threading.Event | None) -> Iterator[None]:
    previous = set_event(event)
    try:
        yield
    finally:
        set_event(previous)


@contextmanager
def deadline_scope(deadline: float | None) -> Iterator[None]:
    previous = current_deadline()
    active = deadline
    if previous is not None and (active is None or previous < active):
        active = previous
    _context.deadline = active
    try:
        yield
    finally:
        _context.deadline = previous


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
    timeout = max(0.0, float(seconds))
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
) -> tuple[subprocess.Popen[bytes], object]:
    """Check cancellation once, then spawn. Callers holding a spawn gate must
    keep the gate across their final Stop check and this call so Stop cannot
    land between check and Popen."""
    check()
    group_args: dict[str, Any]
    if os.name == "nt":
        group_args = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group_args = {"start_new_session": True}
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        shell=shell,
        **group_args,
    )
    job = attach_process_tree(proc)
    return proc, job


def _pump_stream(stream: BinaryIO, state: _StreamPump) -> None:
    """Drain one pipe into its bounded capture; a read failure is sticky."""
    try:
        while True:
            chunk = stream.read(READ_CHUNK_BYTES)
            if not chunk:
                return
            state.capture.feed(chunk)
    except Exception as exc:
        state.error = exc


def _join_readers(threads: list[threading.Thread], *, timeout: float) -> None:
    budget = max(0.0, float(timeout))
    deadline = time.monotonic() + budget
    for thread in threads:
        remaining = deadline - time.monotonic()
        thread.join(timeout=max(0.0, remaining))


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


def wait_process(
    proc: subprocess.Popen[bytes],
    job: object,
    args: str | Sequence[str],
    timeout: float,
    *,
    capture_limit_bytes: int,
) -> CapturedProcess:
    """Wait with bounded per-stream capture; never buffers output unbounded.

    This function owns the whole lifecycle: readers, the process tree, the
    pipes, and the Job handle are all cleaned in one ``finally`` unless the
    run completed. Failure branches only raise; they never clean up
    themselves, so cleanup can neither be skipped nor mask the error.
    """
    limit = max(1, int(capture_limit_bytes))
    head = limit // 4
    tail = limit - head
    stdout_state = _StreamPump(BoundedByteCapture(head_limit=head, tail_limit=tail))
    stderr_state = _StreamPump(BoundedByteCapture(head_limit=head, tail_limit=tail))
    readers: list[threading.Thread] = []
    streams = (
        (proc.stdout, stdout_state),
        (proc.stderr, stderr_state),
    )
    completed = False
    try:
        for stream, state in streams:
            if stream is None:
                continue
            thread = threading.Thread(
                target=_pump_stream, args=(stream, state), daemon=True
            )
            thread.start()
            readers.append(thread)
        deadline = time.monotonic() + max(0.0, float(timeout))
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
        drain_deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        for thread in readers:
            thread.join(timeout=max(0.0, drain_deadline - time.monotonic()))
        if any(thread.is_alive() for thread in readers):
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
        completed = True
        return CapturedProcess(
            args=args,
            returncode=int(returncode),
            stdout=stdout_done.text,
            stderr=stderr_done.text,
            stdout_bytes=stdout_done.total_bytes,
            stderr_bytes=stderr_done.total_bytes,
            stdout_truncated=stdout_done.truncated,
            stderr_truncated=stderr_done.truncated,
        )
    finally:
        if not completed:
            _terminate_process_tree(proc, job)
            _join_readers(readers, timeout=READER_JOIN_TIMEOUT_SECONDS)
        _close_pipes(proc)
        close = getattr(job, "close", None)
        if callable(close):
            with suppress(Exception):
                close()


def run_process(
    args: str | Sequence[str],
    *,
    cwd: str | Path,
    timeout: float,
    env: dict[str, str] | None = None,
    shell: bool = False,
    capture_limit_bytes: int,
) -> CapturedProcess:
    """Run a captured process and terminate its process group when cancelled."""
    proc, job = start_process(args, cwd=cwd, env=env, shell=shell)
    return wait_process(proc, job, args, timeout, capture_limit_bytes=capture_limit_bytes)


def attach_process_tree(proc: subprocess.Popen[bytes]):
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
        raise


def terminate_process_tree(
    proc: subprocess.Popen[bytes],
    job=None,
) -> None:
    """Terminate a process and its children created in the same tree/group."""
    _terminate_process_tree(proc, job)


def _process_group_exists(pgid: int) -> bool:
    """Check a process group without touching its (possibly reaped) leader."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _terminate_process_tree(
    proc: subprocess.Popen[bytes],
    job: _WindowsJob | None = None,
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
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            with suppress(Exception):
                proc.terminate()
        with suppress(Exception):
            proc.wait(timeout=1)
        # proc.wait() only reaps the direct child; grandchildren holding
        # pipes are judged by group existence, never by the parent's wait.
        if _process_group_exists(pgid):
            with suppress(Exception):
                os.killpg(pgid, signal.SIGKILL)
            with suppress(Exception):
                proc.wait(timeout=1)
