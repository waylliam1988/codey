"""Exercise the formal desktop entry with an actual listening socket."""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

from codey.app import server
from codey.storage.file_lock import acquire_lease


def test_desktop_exit_closes_listener_and_releases_lease(tmp_path, monkeypatch):
    instances = []
    server_type = server.CodeyHTTPServer

    def create_server(*args, **kwargs):
        instance = server_type(*args, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(server, "DEFAULT_STATE_HOME", tmp_path)
    monkeypatch.setattr(server, "CodeyHTTPServer", create_server)
    monkeypatch.setattr(server, "get_state", lambda: object())
    fake_webview = mock.Mock()
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    warmup = mock.Mock(side_effect=AssertionError("Startup must never open model websites"))
    monkeypatch.setattr(server.provider_services, "start_provider_warmup", warmup)
    try:
        server.serve(port=0)
        warmup.assert_not_called()
        fake_webview.start.assert_called_once()
        assert fake_webview.create_window.call_args.kwargs.get("text_select") is True
        assert len(instances) == 1
        assert instances[0].socket.fileno() == -1
        with acquire_lease(tmp_path / ".server.lock"):
            pass
    finally:
        for instance in instances:
            instance.server_close()


def test_http_thread_start_failure_cannot_deadlock_shutdown(tmp_path):
    script = """
import sys
from pathlib import Path
from unittest import mock
from codey.app import server
from codey.storage.file_lock import acquire_lease

instances = []
server_type = server.CodeyHTTPServer
def create_server(*args, **kwargs):
    instance = server_type(*args, **kwargs)
    instances.append(instance)
    return instance
server.DEFAULT_STATE_HOME = Path(sys.argv[1])
with mock.patch.object(server, 'CodeyHTTPServer', create_server), \\
     mock.patch.object(server.threading.Thread, 'start', side_effect=RuntimeError('thread unavailable')):
    try:
        server.serve(port=0)
    except RuntimeError as exc:
        assert str(exc) == 'thread unavailable'
    else:
        raise AssertionError('startup failure was swallowed')
assert instances[0].socket.fileno() == -1
with acquire_lease(server.DEFAULT_STATE_HOME / '.server.lock'):
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
