"""Windows spawn ownership is established before a child can execute."""
from __future__ import annotations

import io
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.runtime.core import cancellation


@dataclass
class ProcessDouble:
    stdout: io.BytesIO = field(default_factory=io.BytesIO)
    stderr: io.BytesIO = field(default_factory=io.BytesIO)
    stdin: None = None
    pid: int = 123
    completed: bool = False
    owned: bool = False
    kills: int = 0

    def kill(self) -> None:
        self.kills += 1

    def wait(self, timeout: float | None = None) -> int:
        return 0


@dataclass
class JobDouble:
    terminations: int = 0
    closes: int = 0

    def terminate(self) -> None:
        self.terminations += 1

    def close(self) -> None:
        self.closes += 1


def windows_process_fixture(monkeypatch: pytest.MonkeyPatch, *, resume_status: int = 0) -> tuple[ProcessDouble, JobDouble, list[str]]:
    events: list[str] = []
    proc, job = ProcessDouble(), JobDouble()

    def popen(*args: object, **kwargs: object) -> ProcessDouble:
        env = kwargs['env']
        assert isinstance(env, dict)
        assert env['PYTHONUTF8'] == '1' and env['PYTHONIOENCODING'] == 'utf-8'
        events.append('spawn')
        flags = kwargs['creationflags']
        assert isinstance(flags, int)
        if not flags & 0x4:
            proc.completed = True
            events.append('execute')
        return proc

    def attach(child: object) -> JobDouble:
        assert child is proc
        events.append('attach')
        if proc.completed:
            raise PermissionError(5, 'process already exited before Job assignment')
        proc.owned = True
        return job

    def resume(child: object) -> None:
        assert child is proc and proc.owned
        events.append('resume')
        if resume_status:
            raise OSError(5, 'resume rejected')

    def terminate(child: object, owner: object = None) -> None:
        assert child is proc and owner is job
        job.terminate()
        proc.kill()
        proc.wait(timeout=1)

    monkeypatch.setattr(cancellation, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(cancellation, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(cancellation, 'subprocess', SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL, CREATE_NEW_PROCESS_GROUP=512))
    monkeypatch.setattr(cancellation, 'attach_process_tree', attach)
    monkeypatch.setattr(cancellation, '_resume_windows_process', resume)
    monkeypatch.setattr(cancellation, 'terminate_process_tree', terminate)
    return proc, job, events


def test_fast_command_is_suspended_until_job_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, job, events = windows_process_fixture(monkeypatch)
    spawned, owner = cancellation.start_process(['git', 'rev-parse', 'HEAD'], cwd='.')
    assert id(spawned) == id(proc) and owner is job
    assert events == ['spawn', 'attach', 'resume']
    assert not proc.completed and proc.kills == job.closes == 0


def test_resume_failure_kills_owned_process_and_closes_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, job, events = windows_process_fixture(monkeypatch, resume_status=-1073741790)
    with pytest.raises(OSError, match='resume rejected'):
        cancellation.start_process(['git', 'rev-parse', 'HEAD'], cwd='.')
    assert events == ['spawn', 'attach', 'resume']
    assert job.terminations == job.closes == proc.kills == 1
    assert proc.stdout.closed and proc.stderr.closed


def test_cancel_after_attachment_never_resumes_and_releases_owned_process(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, job, events = windows_process_fixture(monkeypatch)
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise cancellation.TaskCancelled('stopped')

    monkeypatch.setattr(cancellation, 'check', check)
    with pytest.raises(cancellation.TaskCancelled, match='stopped'):
        cancellation.start_process(['git', 'rev-parse', 'HEAD'], cwd='.')
    assert events == ['spawn', 'attach']
    assert job.terminations == job.closes == proc.kills == 1
    assert proc.stdout.closed and proc.stderr.closed


def test_real_fast_commands_keep_exit_status_and_output(tmp_path: Path) -> None:
    for _ in range(12):
        result = cancellation.run_process([sys.executable, '-c', "print('FAST_OK')"], cwd=tmp_path, timeout=10, capture_limit_bytes=1024)
        assert result.returncode == 0 and result.stdout.strip() == 'FAST_OK'
