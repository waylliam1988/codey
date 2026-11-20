"""Typed Win32 process-tree and ConPTY primitives; no task or scheduling state."""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO


class _Structure(ctypes.Structure):
    def __init__(self) -> None:
        super().__init__()


class _IoCounters(_Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimit(_Structure):
    LimitFlags: int
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimit(_Structure):
    BasicLimitInformation: _BasicLimit
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _StartupInfo(_Structure):
    cb: int
    dwFlags: int
    hStdInput: int
    hStdOutput: int
    hStdError: int
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        *[(name, wintypes.DWORD) for name in ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars",
                                           "dwYCountChars", "dwFillAttribute", "dwFlags")],
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _StartupInfoEx(_Structure):
    StartupInfo: _StartupInfo
    lpAttributeList: int
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(_Structure):
    hProcess: int | None
    hThread: int | None
    dwProcessId: int
    dwThreadId: int
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
               ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class WinAPI:
    """Give each foreign function its actual Python call signature.

    Windows x64/ARM64 use one native calling convention. Reject 32-bit hosts
    rather than incorrectly calling stdcall exports with cdecl prototypes.
    """

    def __init__(self) -> None:
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError("native Windows process I/O requires 64-bit Windows")
        library = ctypes.CDLL("kernel32")
        self.error: Callable[[], int] = ctypes.CFUNCTYPE(wintypes.DWORD)(("GetLastError", library))
        self.close: Callable[[int], int] = ctypes.CFUNCTYPE(wintypes.BOOL, wintypes.HANDLE)(("CloseHandle", library))
        self.open_process: Callable[[int, int, int], int | None] = ctypes.CFUNCTYPE(
            wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)(("OpenProcess", library))
        self.create_job: Callable[[None, None], int | None] = ctypes.CFUNCTYPE(
            wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)(("CreateJobObjectW", library))
        self.set_job: Callable[[int, int, object, int], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)(
                ("SetInformationJobObject", library))
        self.assign_job: Callable[[int, int], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)(("AssignProcessToJobObject", library))
        self.terminate_job: Callable[[int, int], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)(("TerminateJobObject", library))
        self.resume_thread: Callable[[int], int] = ctypes.CFUNCTYPE(
            wintypes.DWORD, wintypes.HANDLE)(("ResumeThread", library))
        self.resume_process: Callable[[int], int] = ctypes.CFUNCTYPE(ctypes.c_long, wintypes.HANDLE)(
            ("NtResumeProcess", ctypes.CDLL("ntdll")))
        self.pipe: Callable[[object, object, None, int], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD)(("CreatePipe", library))
        self.pipe_mode: Callable[[int, object, None, None], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
                ("SetNamedPipeHandleState", library))
        self.create_console: Callable[[int, int, int, int, object], int] = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_uint32, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p)(
                ("CreatePseudoConsole", library))
        self.resize_console: Callable[[int, int], int] = ctypes.CFUNCTYPE(
            ctypes.c_long, wintypes.HANDLE, ctypes.c_uint32)(("ResizePseudoConsole", library))
        self.close_console: Callable[[int], None] = ctypes.CFUNCTYPE(None, wintypes.HANDLE)(
            ("ClosePseudoConsole", library))
        self.init_attributes: Callable[[object, int, int, object], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)(
                ("InitializeProcThreadAttributeList", library))
        self.update_attribute: Callable[[object, int, int, int, int, None, None], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)(("UpdateProcThreadAttribute", library))
        self.delete_attributes: Callable[[object], None] = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(
            ("DeleteProcThreadAttributeList", library))
        self.create_process: Callable[[None, object, None, None, int, int, object, str, object, object], int] = (
            ctypes.CFUNCTYPE(wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p)(
                    ("CreateProcessW", library)))
        self.wait: Callable[[int, int], int] = ctypes.CFUNCTYPE(
            wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)(("WaitForSingleObject", library))
        self.exit_code: Callable[[int, object], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p)(("GetExitCodeProcess", library))
        self.terminate: Callable[[int, int], int] = ctypes.CFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)(("TerminateProcess", library))

    def check(self, value: int) -> None:
        if not value:
            raise OSError(self.error(), "Windows process operation failed")


class WindowsJob:
    def __init__(self, pid: int) -> None:
        self.api = WinAPI()
        self.handle = self.api.create_job(None, None)
        if self.handle is None:
            raise OSError(self.api.error(), "CreateJobObjectW failed")
        process = self.api.open_process(0x0100 | 0x0001, 0, pid)
        try:
            if process is None:
                raise OSError(self.api.error(), "OpenProcess for job assignment failed")
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000
            self.api.check(self.api.set_job(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            self.api.check(self.api.assign_job(self.handle, process))
        except BaseException:
            self.close()
            raise
        finally:
            if process is not None:
                self.api.close(process)

    def terminate(self) -> None:
        if self.handle is not None and not self.api.terminate_job(self.handle, 1):
            self.close()

    def close(self) -> None:
        if self.handle is not None:
            self.api.close(self.handle)
            self.handle = None


def resume_process(pid: int) -> None:
    api = WinAPI()
    handle = api.open_process(0x0800, 0, pid)
    if handle is None:
        raise OSError(api.error(), "OpenProcess for resume failed")
    try:
        if api.resume_process(handle) < 0:
            raise OSError("NtResumeProcess failed")
    finally:
        api.close(handle)


def nonblocking_pipe(fd: int) -> None:
    import msvcrt

    api = WinAPI()
    mode = wintypes.DWORD(1)  # PIPE_NOWAIT; do not block the controller or cancellation.
    api.check(api.pipe_mode(msvcrt.get_osfhandle(fd), ctypes.byref(mode), None, None))


class ConPTYProcess:
    """Native process adapter used by the same bounded process collector as Popen."""

    def __init__(self, args: str | Sequence[str], cwd: str | Path, env: dict[str, str], cols: int, rows: int) -> None:
        self.api = WinAPI()
        self.args = args
        self.pid = 0
        self.handle: int | None = None
        self.console: int | None = None
        self.stdin: BinaryIO | None = None
        self.stdout: BinaryIO | None = None
        self.stderr: BinaryIO | None = None
        self.job: WindowsJob | None = None
        self._spawn(cwd, env, cols, rows)

    def _spawn(self, cwd: str | Path, env: dict[str, str], cols: int, rows: int) -> None:
        import msvcrt

        handles: list[int] = []
        attributes = None
        initialized = False
        info = _ProcessInfo()
        try:
            input_read, input_write, output_read, output_write = (wintypes.HANDLE() for _ in range(4))
            for read, write in ((input_read, input_write), (output_read, output_write)):
                self.api.check(self.api.pipe(ctypes.byref(read), ctypes.byref(write), None, 65536))
                if read.value is None or write.value is None:
                    raise OSError("pipe returned no handle")
                handles.extend((read.value, write.value))
            console = wintypes.HANDLE()
            if self.api.create_console(cols | rows << 16, handles[0], handles[3], 0, ctypes.byref(console)) < 0:
                raise OSError("CreatePseudoConsole failed")
            self.console = console.value
            if self.console is None:
                raise OSError("CreatePseudoConsole returned no handle")
            size = ctypes.c_size_t()
            self.api.init_attributes(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            self.api.check(self.api.init_attributes(attributes, 1, 0, ctypes.byref(size)))
            initialized = True
            self.api.check(self.api.update_attribute(attributes, 0, 0x00020016, self.console,
                                                    ctypes.sizeof(wintypes.HANDLE), None, None))
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # Prevent redirected host standard handles from bypassing ConPTY.
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = -1
            startup.StartupInfo.hStdOutput = -1
            startup.StartupInfo.hStdError = -1
            startup.lpAttributeList = ctypes.addressof(attributes)
            command = ctypes.create_unicode_buffer(self.args if isinstance(self.args, str) else subprocess.list2cmdline(self.args))
            environment = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
            self.api.check(self.api.create_process(None, command, None, None, 0, 0x80000 | 0x400 | 0x4,
                environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)))
            self.handle = info.hProcess
            self.pid = info.dwProcessId
            self.job = WindowsJob(self.pid)
            input_handle, output_handle = handles[1], handles[2]
            self.stdin = os.fdopen(msvcrt.open_osfhandle(input_handle, os.O_WRONLY | os.O_BINARY), "wb", buffering=0)
            handles.remove(input_handle)
            self.stdout = os.fdopen(msvcrt.open_osfhandle(output_handle, os.O_RDONLY | os.O_BINARY), "rb", buffering=0)
            handles.remove(output_handle)
            if info.hThread is None or self.api.resume_thread(info.hThread) == 0xFFFFFFFF:
                raise OSError("ResumeThread failed")
        except BaseException:
            self.kill()
            for stream in (self.stdin, self.stdout):
                if stream is not None:
                    stream.close()
            self.close()
            raise
        finally:
            if info.hThread:
                self.api.close(info.hThread)
            if initialized and attributes is not None:
                self.api.delete_attributes(attributes)
            for handle in handles:
                self.api.close(handle)

    def poll(self) -> int | None:
        if self.handle is None:
            return None
        if self.api.wait(self.handle, 0) == 0x102:
            return None
        code = wintypes.DWORD()
        self.api.check(self.api.exit_code(self.handle, ctypes.byref(code)))
        return code.value

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                self._close_console()
                return code
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout if timeout is not None else 0)
            time.sleep(0.01)

    def kill(self) -> None:
        if self.job is not None:
            self.job.terminate()
        elif self.handle is not None:
            self.api.terminate(self.handle, 1)

    def terminate(self) -> None:
        self.kill()

    def resize(self, cols: int, rows: int) -> None:
        if self.console is None:
            raise ValueError("terminal is closed")
        if self.api.resize_console(self.console, cols | rows << 16) < 0:
            raise OSError("ResizePseudoConsole failed")

    def _close_console(self) -> None:
        if self.console is not None:
            console, self.console = self.console, None
            self.api.close_console(console)

    def close(self) -> None:
        self._close_console()
        if self.job is not None:
            self.job.close()
        if self.handle is not None:
            self.api.close(self.handle)
            self.handle = None
