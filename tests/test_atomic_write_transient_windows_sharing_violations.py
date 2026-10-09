"""Windows sharing failures retry one atomic operation, never a tool execution."""
import os
from unittest import mock

import pytest

from codey.storage.atomic_io import write_bytes_atomic


def sharing_violation():
    error = PermissionError('file is temporarily open')
    error.winerror = 32
    return error


def test_transient_sharing_violation_retries_same_temp_and_commits_once(tmp_path):
    target = tmp_path / 'app.py'
    target.write_bytes(b'old')
    replace = os.replace
    calls = []

    def transient(source, destination):
        calls.append((source, destination))
        if len(calls) == 1:
            raise sharing_violation()
        return replace(source, destination)

    with mock.patch('codey.storage.atomic_io.os.replace', side_effect=transient):
        write_bytes_atomic(target, b'new')
    assert target.read_bytes() == b'new'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ['app.py']


def test_failed_replace_removes_temp_after_transient_cleanup_sharing_violation(tmp_path):
    target = tmp_path / 'app.py'
    target.write_bytes(b'old')
    from pathlib import Path
    unlink = Path.unlink
    calls = []

    def transient(path, *args, **kwargs):
        calls.append(path)
        if len(calls) <= 2:
            raise sharing_violation()
        return unlink(path, *args, **kwargs)

    with (mock.patch('codey.storage.atomic_io.os.replace', side_effect=OSError('disk failure')),
          mock.patch('codey.storage.atomic_io.Path.unlink', autospec=True, side_effect=transient),
          pytest.raises(OSError, match='disk failure')):
        write_bytes_atomic(target, b'new')
    assert target.read_bytes() == b'old'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['app.py']


def test_permission_denial_is_not_retried_or_hidden(tmp_path):
    target = tmp_path / 'app.py'
    with (mock.patch('codey.storage.atomic_io.os.replace', side_effect=PermissionError('denied')) as replace,
          pytest.raises(PermissionError, match='denied')):
        write_bytes_atomic(target, b'new')
    assert replace.call_count == 1 and not list(tmp_path.iterdir())


def test_permanent_sharing_failure_is_bounded_and_retains_original(tmp_path):
    target = tmp_path / 'app.py'
    target.write_bytes(b'old')
    with (mock.patch('codey.storage.atomic_io.os.replace', side_effect=sharing_violation()) as replace,
          pytest.raises(PermissionError)):
        write_bytes_atomic(target, b'new')
    assert 1 < replace.call_count <= 10
    assert target.read_bytes() == b'old'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['app.py']


def test_target_changed_during_sharing_failure_is_not_overwritten_by_retry(tmp_path):
    target = tmp_path / 'app.py'
    target.write_bytes(b'old')
    replace = os.replace
    calls = []

    def transient(source, destination):
        calls.append((source, destination))
        if len(calls) == 1:
            target.write_bytes(b'concurrent editor change')
            raise sharing_violation()
        return replace(source, destination)

    with mock.patch('codey.storage.atomic_io.os.replace', side_effect=transient), pytest.raises(OSError):
        write_bytes_atomic(target, b'new')
    assert target.read_bytes() == b'concurrent editor change'
    assert len(calls) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ['app.py']
