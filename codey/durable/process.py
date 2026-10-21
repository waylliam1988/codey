"""Read-only process ownership checks for durable scheduler fences."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if os.name == "nt" or TYPE_CHECKING:
    import ctypes
    from ctypes import wintypes

    # Windows-only ctypes entry points are absent from Linux typeshed.
    # Keep this dynamic adapter available to mypy without importing it at
    # runtime on POSIX, where the code path is never reached.
    _windows_ctypes: Any = ctypes


def process_is_alive(value: object) -> bool:
    """Probe a PID without sending Windows console control events."""
    if type(value) is not int or value <= 0:
        return False
    if value == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_is_alive(value)
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:
    if pid > 0xFFFFFFFF:
        return False
    kernel32 = _windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        # Access denied does not authorize stealing a live process's fence.
        return bool(_windows_ctypes.get_last_error() == 5)
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, _windows_ctypes.byref(code)):
            raise _windows_ctypes.WinError(_windows_ctypes.get_last_error())
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)
