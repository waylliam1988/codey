"""Task-local cooperative cancellation shared by blocking runtime paths."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


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

    def __init__(self, proc: subprocess.Popen[str]) -> None:
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


def run_process(
    args: str | Sequence[str],
    *,
    cwd: str | Path,
    timeout: float,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a captured process and terminate its process group when cancelled."""
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
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=shell,
        **group_args,
    )
    job = attach_process_tree(proc)
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = proc.communicate(
                    timeout=min(POLL_INTERVAL, remaining),
                )
                return subprocess.CompletedProcess(
                    args,
                    proc.returncode,
                    stdout,
                    stderr,
                )
            except subprocess.TimeoutExpired:
                continue
    except (TaskCancelled, DeadlineExceeded, subprocess.TimeoutExpired):
        terminate_process_tree(proc, job)
        raise
    finally:
        if job is not None:
            job.close()


def attach_process_tree(proc: subprocess.Popen[str]):
    """Attach a process to the platform process-tree owner, when available."""
    if os.name != "nt":
        return None
    try:
        return _WindowsJob(proc)
    except Exception:
        proc.kill()
        proc.communicate()
        raise


def terminate_process_tree(
    proc: subprocess.Popen[str],
    job=None,
) -> None:
    """Terminate a process and its children created in the same tree/group."""
    _terminate_process_tree(proc, job)


def _terminate_process_tree(
    proc: subprocess.Popen[str],
    job: _WindowsJob | None = None,
) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        if job is not None:
            job.terminate()
        if proc.poll() is None:
            proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
    try:
        proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
