"""The next desktop launch must keep the committed runtime after an upgrade."""

import json
from pathlib import Path
from types import SimpleNamespace

from codey.control.client import ControlClient, ensure_service
from codey.control.protocol import ControlError
from codey.launcher.releases import snapshot


def test_desktop_relaunch_uses_the_committed_immutable_runtime(tmp_path, monkeypatch):
    active = snapshot(tmp_path, Path.cwd())
    (tmp_path / "active-release.json").write_text(json.dumps(active))
    calls = []
    health_checks = []

    def health(self, *args, **kwargs):
        health_checks.append(True)
        if len(health_checks) == 1:
            raise ControlError("NOT_FOUND", "stopped service")
        return {"status": "ready"}

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(ControlClient, "call", health)
    monkeypatch.setattr("codey.control.client.subprocess.Popen", start)
    ensure_service(tmp_path)
    argv, options = calls[0]
    assert Path(options["cwd"]) == Path(active["source"])
    assert argv[argv.index("--release-id") + 1] == active["release_id"]
