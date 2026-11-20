from __future__ import annotations

import os
import queue
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from codey.runtime.core import cancellation
from codey.runtime.core.process_io import ProcessIO, ProcessOptions


class RunningProcess:
    def __init__(self, root: Path, script: str, *, tty: bool = False) -> None:
        self.stop = threading.Event()
        self.outputs: list[cancellation.CapturedProcess] = []
        self.errors: list[BaseException] = []
        self.ready: queue.Queue[ProcessIO] = queue.Queue()

        def started(endpoint: ProcessIO) -> Callable[[], None]:
            self.ready.put(endpoint)
            return lambda: None

        def run() -> None:
            try:
                with cancellation.scope(self.stop):
                    self.outputs.append(cancellation.run_process([sys.executable, "-u", "-c", script],
                        cwd=root, timeout=15, capture_limit_bytes=65536,
                        options=ProcessOptions(stdin=True, tty=tty), on_started=started))
            except BaseException as exc:
                self.errors.append(exc)

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def endpoint(self) -> ProcessIO:
        try:
            return self.ready.get(timeout=5)
        except queue.Empty:
            raise AssertionError(f"process did not start: {self.errors}") from None

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=8)
        assert not self.thread.is_alive(), "process owner failed to clean up"


def test_pipe_input_eof_and_reject_after_exit(tmp_path: Path) -> None:
    running = RunningProcess(tmp_path, "import sys; a=sys.stdin.readline(); b=sys.stdin.read(); print(repr((a,b)))")
    try:
        endpoint = running.endpoint()
        first = endpoint.write_input("你好\n".encode(), eof=False, valid=lambda: True)
        assert first.complete and first.bytes_written == len("你好\n".encode())
        assert endpoint.write_input(b"second", eof=True, valid=lambda: True).complete
        running.thread.join(timeout=5)
        assert not running.errors, running.errors
        assert len(running.outputs) == 1
        assert "second" in running.outputs[0].stdout
        with pytest.raises(ValueError, match="stopped"):
            endpoint.write_input(b"late", eof=False, valid=lambda: True)
    finally:
        running.close()


def test_input_is_bounded_when_program_does_not_read(tmp_path: Path) -> None:
    running = RunningProcess(tmp_path, "import time; time.sleep(30)")
    try:
        endpoint = running.endpoint()
        delivery = endpoint.write_input(b"x" * 65536, eof=False, valid=lambda: True, timeout=0.1)
        assert not delivery.complete and delivery.bytes_written < 65536
        with pytest.raises(ValueError, match="old attempt"):
            endpoint.write_input(b"late", eof=False, valid=lambda: False)
    finally:
        running.close()


@pytest.mark.parametrize("tty", [False, True])
def test_failed_resource_registration_cleans_started_process(tmp_path: Path, tty: bool) -> None:
    endpoints: list[ProcessIO] = []

    def reject(endpoint: ProcessIO) -> Callable[[], None]:
        endpoints.append(endpoint)
        raise RuntimeError("resource registration failed")

    with pytest.raises(RuntimeError, match="resource registration failed"):
        cancellation.run_process([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path,
            timeout=10, capture_limit_bytes=4096, options=ProcessOptions(stdin=True, tty=tty), on_started=reject)
    assert len(endpoints) == 1
    assert endpoints[0].writer is None
    from codey.runtime.core.windows_process import ConPTYProcess

    process = endpoints[0].process
    if isinstance(process, ConPTYProcess):
        assert process.handle is None and process.console is None
    else:
        assert process.poll() is not None


def test_real_terminal_prompt_size_and_input(tmp_path: Path) -> None:
    script = ("import os,sys; print('TTY',sys.stdin.isatty(),sys.stdout.isatty(),flush=True);"
              "print('prompt> ',end='',flush=True); line=input(); print('INPUT='+line,flush=True);"
              "print('SIZE',os.get_terminal_size().columns,os.get_terminal_size().lines,flush=True)")
    running = RunningProcess(tmp_path, script, tty=True)
    try:
        endpoint = running.endpoint()
        with pytest.raises(ValueError, match="mode-dependent"):
            endpoint.write_input(b"", eof=True, valid=lambda: True)
        endpoint.resize(91, 37, lambda: True)
        enter = b"hello\r" if os.name == "nt" else b"hello\n"
        assert endpoint.write_input(enter, eof=False, valid=lambda: True).complete
        running.thread.join(timeout=8)
        assert not running.errors, running.errors
        assert len(running.outputs) == 1
        output = running.outputs[0]
        assert output.returncode == 0
        visible = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.stdout)
        assert "TTY True True" in visible, output.stdout
        assert "prompt>" in output.stdout and "INPUT=hello" in output.stdout
        assert "91" in output.stdout and "37" in output.stdout
        assert output.stderr == ""
        with pytest.raises(ValueError):
            endpoint.resize(10, 10, lambda: True)
    finally:
        running.close()


def test_terminal_interrupt_leaves_program_available_for_more_input(tmp_path: Path) -> None:
    script = """import signal,time,pathlib
interrupted = False
def handle(signum, frame):
    global interrupted
    interrupted = True
signal.signal(signal.SIGINT, handle)
pathlib.Path('ready').write_text('ready')
while not interrupted:
    time.sleep(0.02)
print('INTERRUPTED', flush=True)
assert input() == 'done'
print('CONTINUED', flush=True)
"""
    running = RunningProcess(tmp_path, script, tty=True)
    try:
        endpoint = running.endpoint()
        for _ in range(200):
            if (tmp_path / "ready").exists():
                break
            threading.Event().wait(0.02)
        assert (tmp_path / "ready").exists()
        endpoint.interrupt(lambda: True)
        assert endpoint.write_input(b"done\r" if os.name == "nt" else b"done\n",
                                    eof=False, valid=lambda: True).complete
        running.thread.join(timeout=8)
        assert not running.errors, running.errors
        assert len(running.outputs) == 1
        assert running.outputs[0].returncode == 0
        assert "INTERRUPTED" in running.outputs[0].stdout
        assert "CONTINUED" in running.outputs[0].stdout
    finally:
        running.close()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ConPTY and Job Object")
def test_conpty_cancel_cleans_real_process_tree(tmp_path: Path) -> None:
    script = ("import subprocess,sys,time,pathlib; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "pathlib.Path('descendant.pid').write_text(str(p.pid)); time.sleep(30)")
    running = RunningProcess(tmp_path, script, tty=True)
    try:
        running.endpoint()
        for _ in range(150):
            if (tmp_path / "descendant.pid").exists():
                break
            threading.Event().wait(0.02)
        pid = int((tmp_path / "descendant.pid").read_text())
    finally:
        running.close()
    from codey.runtime.core.windows_process import WinAPI

    api = WinAPI()
    handle = api.open_process(0x1000 | 0x100000, 0, pid)
    if handle is not None:
        try:
            assert api.wait(handle, 0) == 0
        finally:
            api.close(handle)
