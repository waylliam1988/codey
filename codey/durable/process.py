"""Read-only process ownership checks for durable scheduler fences."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from ctypes import wintypes


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


if sys.platform == "win32":
    def _windows_process_is_alive(pid: int) -> bool:
        if pid > 0xFFFFFFFF:
            return False
        kernel32 = ctypes.WinDLL("kernel32")
        open_process: Callable[[int, bool, int], int | None] = ctypes.WINFUNCTYPE(
            wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD, use_last_error=True,
        )(("OpenProcess", kernel32))
        wait: Callable[[int, int], int] = ctypes.WINFUNCTYPE(
            wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD, use_last_error=True,
        )(("WaitForSingleObject", kernel32))
        close: Callable[[int], int] = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE,
        )(("CloseHandle", kernel32))

        synchronize = 0x00100000
        handle = open_process(synchronize, False, pid)
        if not handle:
            # Access denied does not authorize stealing a live process's fence.
            return ctypes.get_last_error() == 5
        try:
            # The handle is signaled on exit, regardless of the process exit code.
            result = wait(handle, 0)
            if result == 0xFFFFFFFF:  # WAIT_FAILED
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 0x102  # WAIT_TIMEOUT
        finally:
            close(handle)

else:
    def _windows_process_is_alive(pid: int) -> bool:
        raise OSError("Windows process probing requires Windows")
