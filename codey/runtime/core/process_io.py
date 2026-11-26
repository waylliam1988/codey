"""Live process I/O adapters shared by the existing execution and collection owner."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from codey.runtime.core.cancellation import ProcessTreeOwner


class ReadPipe(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...
    def close(self) -> None: ...


class ChildProcess(Protocol):
    @property
    def pid(self) -> int: ...
    @property
    def args(self) -> object: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...
    def terminate(self) -> None: ...


class Process(ChildProcess, Protocol):
    @property
    def stdout(self) -> ReadPipe | None: ...
    @property
    def stderr(self) -> ReadPipe | None: ...


@dataclass(frozen=True)
class ProcessOptions:
    stdin: bool = False
    tty: bool = False
    cols: int = 100
    rows: int = 30
    hide_console: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.cols <= 1000 or not 1 <= self.rows <= 1000:
            raise ValueError("terminal dimensions must be between 1 and 1000")


@dataclass(frozen=True)
class InputDelivery:
    bytes_written: int
    eof: bool
    complete: bool


class ProcessIO:
    """Only live handles and I/O synchronization; durable owns task state."""

    def __init__(self, process: Process, writer: IO[bytes] | None, *, terminal: bool = False,
                 resize: Callable[[int, int], None] | None = None) -> None:
        self.process = process
        self.writer = writer
        self.terminal = terminal
        self._resize = resize
        self._lock = threading.Lock()
        self._revoked = threading.Event()
        if writer is not None:
            if sys.platform == "win32":
                from codey.runtime.core.windows_process import nonblocking_pipe

                nonblocking_pipe(writer.fileno())
            else:
                os.set_blocking(writer.fileno(), False)

    def write_input(self, data: bytes, *, eof: bool, valid: Callable[[], bool], timeout: float = 2) -> InputDelivery:
        from codey.runtime.core import cancellation

        if len(data) > 65536:
            raise ValueError("input exceeds 65536 UTF-8 bytes")
        deadline = time.monotonic() + timeout
        with self._lock:
            self._check_live(valid)
            writer = self.writer
            if writer is None:
                raise ValueError("process input is closed or unavailable")
            if eof and self.terminal:
                raise ValueError("terminal EOF is mode-dependent; send raw keys or cancel the task")
            written = 0
            while written < len(data):
                if self._revoked.is_set() or not valid() or self.process.poll() is not None:
                    return InputDelivery(written, False, False)
                try:
                    count = os.write(writer.fileno(), data[written:written + 4096])
                except BlockingIOError:
                    count = 0
                except OSError:
                    return InputDelivery(written, False, False)
                written += count
                if written < len(data):
                    if time.monotonic() >= deadline:
                        return InputDelivery(written, False, False)
                    try:
                        cancellation.wait(0.01)
                    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                        return InputDelivery(written, False, False)
            if eof:
                writer.close()
                self.writer = None
            return InputDelivery(written, eof, True)

    def interrupt(self, valid: Callable[[], bool]) -> None:
        if not self.terminal:
            raise ValueError("interrupt requires a terminal")
        delivery = self.write_input(b"\x03", eof=False, valid=valid)
        if not delivery.complete:
            raise OSError("terminal interrupt delivery is uncertain")

    def resize(self, cols: int, rows: int, valid: Callable[[], bool]) -> None:
        if not 1 <= cols <= 1000 or not 1 <= rows <= 1000:
            raise ValueError("terminal dimensions must be between 1 and 1000")
        with self._lock:
            self._check_live(valid)
            if self._resize is None:
                raise ValueError("process has no resizable terminal")
            self._resize(cols, rows)

    def _check_live(self, valid: Callable[[], bool]) -> None:
        if self._revoked.is_set() or not valid() or self.process.poll() is not None:
            raise ValueError("process is stopped, cancelled or belongs to an old attempt")

    def close(self) -> None:
        self._revoked.set()
        with self._lock:
            if self.writer is not None:
                self.writer.close()
                self.writer = None


class _PTYReader:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def read(self, size: int = -1, /) -> bytes:
        while True:
            try:
                return os.read(self.fd, size if size > 0 else 32768)
            except BlockingIOError:
                time.sleep(0.01)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return b""  # POSIX PTY slave has closed.
                raise

    def close(self) -> None:
        os.close(self.fd)


class _PTYProcess:
    def __init__(self, proc: subprocess.Popen[bytes], reader: ReadPipe) -> None:
        self.proc = proc
        self.stdout = reader
        self.stderr: ReadPipe | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def args(self) -> object:
        return self.proc.args

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout)

    def kill(self) -> None:
        self.proc.kill()

    def terminate(self) -> None:
        self.proc.terminate()


def start_terminal(args: str | Sequence[str], *, cwd: str | Path, env: dict[str, str],
                   options: ProcessOptions) -> tuple[Process, ProcessTreeOwner | None, ProcessIO]:
    if os.name == "nt":
        from codey.runtime.core.windows_process import ConPTYProcess

        native = ConPTYProcess(args, cwd, env, options.cols, options.rows)
        try:
            endpoint = ProcessIO(native, native.stdin, terminal=True, resize=native.resize)
        except BaseException:
            native.kill()
            native.close()
            raise
        return native, native.job, endpoint
    return _start_posix_terminal(args, cwd=cwd, env=env, options=options)


if sys.platform != "win32":
    def _start_posix_terminal(args: str | Sequence[str], *, cwd: str | Path, env: dict[str, str],
                               options: ProcessOptions) -> tuple[Process, ProcessTreeOwner | None, ProcessIO]:
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", options.rows, options.cols, 0, 0))
            # A small exec wrapper acquires the controlling terminal after setsid;
            # preexec_fn is unsafe in this multithreaded application.
            script = ("import fcntl,os,sys,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);"
                      "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
            argv = ["/bin/sh", "-c", args] if isinstance(args, str) else list(args)
            proc = subprocess.Popen([sys.executable, "-c", script, *argv], cwd=cwd, env=env,
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        reader = _PTYReader(master)
        process = _PTYProcess(proc, reader)
        writer: IO[bytes] | None = None
        try:
            writer = os.fdopen(os.dup(master), "wb", buffering=0)

            def resize(cols: int, rows: int) -> None:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

            return process, None, ProcessIO(process, writer, terminal=True, resize=resize)
        except BaseException:
            from codey.runtime.core.cancellation import terminate_process_tree

            terminate_process_tree(process)
            if writer is not None:
                writer.close()
            reader.close()
            raise
else:
    def _start_posix_terminal(args: str | Sequence[str], *, cwd: str | Path, env: dict[str, str],
                               options: ProcessOptions) -> tuple[Process, ProcessTreeOwner | None, ProcessIO]:
        raise OSError("POSIX PTY is unavailable on Windows")
