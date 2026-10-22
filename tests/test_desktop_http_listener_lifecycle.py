"""The shared service outlives its optional window and owns its listener."""

import sys
from unittest import mock

import pytest

from codey.app.desktop import open_desktop
from codey.control.client import ControlClient
from codey.storage.file_lock import LockTimeout, acquire_lease
from tests.release_helpers import start_local_service


def test_desktop_exit_keeps_shared_service_and_lease(tmp_path):
    runtime = start_local_service(tmp_path)
    try:
        client = ControlClient(tmp_path, operator=True)
        view = mock.Mock()
        with mock.patch("codey.app.desktop.ensure_service", return_value=client), mock.patch.dict(sys.modules, {"webview": view}):
            open_desktop(tmp_path, port=0)
        assert client.call("get_health")["status"] == "ready"
        assert runtime.httpd.socket.fileno() >= 0
        view.start.assert_called_once()
        with pytest.raises(LockTimeout):
            acquire_lease(tmp_path / ".server.lock")
    finally:
        runtime.close()
    with acquire_lease(tmp_path / ".server.lock"):
        pass


def test_http_thread_start_failure_releases_the_listener_and_lease(tmp_path, monkeypatch):
    from codey.app import server

    instances = []
    original = server.CodeyHTTPServer

    def construct(*args):
        httpd = original(*args)
        instances.append(httpd)
        return httpd

    monkeypatch.setattr(server, "CodeyHTTPServer", construct)
    with mock.patch("threading.Thread.start", side_effect=RuntimeError("thread unavailable")), pytest.raises(RuntimeError, match="thread unavailable"):
        start_local_service(tmp_path)
    assert instances[0].socket.fileno() == -1
    with acquire_lease(tmp_path / ".server.lock"):
        pass
