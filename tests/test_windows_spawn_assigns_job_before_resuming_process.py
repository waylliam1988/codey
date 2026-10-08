"""Fast Windows commands cannot exit before process-tree ownership is attached."""
import ctypes
import io
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.runtime.core import cancellation


def windows_process_fixture(monkeypatch, *, resume_status=0):
    events = []
    proc = SimpleNamespace(_handle=123, stdout=io.BytesIO(), stderr=io.BytesIO(),
        kill=Mock(), wait=Mock(return_value=0), completed=False, owned=False)
    job = Mock()

    def popen(*args, **kwargs):
        events.append("spawn")
        if not kwargs["creationflags"] & 0x4:
            proc.completed = True
            events.append("execute")
        return proc

    def attach(child):
        events.append("attach")
        if child.completed:
            raise PermissionError(5, "process already exited before Job assignment")
        child.owned = True
        return job

    def resume(handle):
        assert handle == proc._handle and proc.owned
        events.append("resume")
        return resume_status

    dll = SimpleNamespace(NtResumeProcess=Mock(side_effect=resume),
        RtlNtStatusToDosError=Mock(return_value=5))
    api = SimpleNamespace(WinDLL=Mock(return_value=dll), c_void_p=ctypes.c_void_p,
        c_long=ctypes.c_long, c_ulong=ctypes.c_ulong,
        WinError=lambda code: OSError(code, "resume rejected"))
    monkeypatch.setattr(cancellation, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(cancellation, "subprocess", SimpleNamespace(Popen=popen, PIPE=subprocess.PIPE))
    monkeypatch.setattr(cancellation, "_windows_subprocess", SimpleNamespace(CREATE_NEW_PROCESS_GROUP=512), raising=False)
    monkeypatch.setattr(cancellation, "_windows_ctypes", api, raising=False)
    monkeypatch.setattr(cancellation, "attach_process_tree", attach)
    return proc, job, events, dll


def test_fast_command_is_suspended_until_job_attachment(monkeypatch):
    proc, job, events, _ = windows_process_fixture(monkeypatch)
    assert cancellation.start_process(["git", "rev-parse", "HEAD"], cwd=".") == (proc, job)
    assert events == ["spawn", "attach", "resume"]
    assert not proc.completed
    proc.kill.assert_not_called()
    job.close.assert_not_called()


def test_resume_failure_kills_owned_process_and_closes_handles(monkeypatch):
    proc, job, events, dll = windows_process_fixture(monkeypatch, resume_status=-1073741790)
    with pytest.raises(OSError, match="resume rejected"):
        cancellation.start_process(["git", "rev-parse", "HEAD"], cwd=".")
    assert events == ["spawn", "attach", "resume"]
    dll.RtlNtStatusToDosError.assert_called_once_with(-1073741790)
    job.terminate.assert_called_once()
    job.close.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.stdout.closed and proc.stderr.closed


def test_cancel_after_attachment_never_resumes_and_releases_owned_process(monkeypatch):
    proc, job, events, dll = windows_process_fixture(monkeypatch)
    monkeypatch.setattr(cancellation, "check", Mock(side_effect=[None, cancellation.TaskCancelled("stopped")]))
    with pytest.raises(cancellation.TaskCancelled, match="stopped"):
        cancellation.start_process(["git", "rev-parse", "HEAD"], cwd=".")
    assert events == ["spawn", "attach"]
    dll.NtResumeProcess.assert_not_called()
    job.terminate.assert_called_once()
    job.close.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.stdout.closed and proc.stderr.closed


def test_real_fast_commands_keep_exit_status_and_output(tmp_path):
    for _ in range(12):
        result = cancellation.run_process([sys.executable, "-c", "print('FAST_OK')"],
            cwd=tmp_path, timeout=10, capture_limit_bytes=1024)
        assert result.returncode == 0
        assert result.stdout.strip() == "FAST_OK"
