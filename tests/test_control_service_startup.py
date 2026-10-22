"""The next desktop launch must keep the committed runtime after an upgrade."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
        return {"status": "ready", "release_id": active["release_id"]}

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr("codey.control.client._materialize_current_release", lambda home: active)
    monkeypatch.setattr(ControlClient, "call", health)
    monkeypatch.setattr("codey.control.client.subprocess.Popen", start)
    ensure_service(tmp_path)
    argv, options = calls[0]
    assert Path(options["cwd"]) == Path(active["source"])
    assert argv[argv.index("--release-id") + 1] == active["release_id"]


def test_ensure_service_switches_a_stale_running_release(tmp_path, monkeypatch):
    old = {"release_id": "release_" + "a" * 32, "candidate_digest": "a" * 64}
    current = {"release_id": "release_" + "b" * 32, "candidate_digest": "b" * 64,
               "source": str(tmp_path / "releases" / ("release_" + "b" * 32) / "source")}
    calls = []
    responses = iter([
        {"release_id": old["release_id"], "instance_id": "old-instance", "status": "ready"},
        {"phase": "active"},
        {"release_id": current["release_id"], "status": "ready"},
    ])

    monkeypatch.setattr("codey.control.client._materialize_current_release", lambda home: current)
    monkeypatch.setattr("codey.control.client._current_source", lambda: Path.cwd())
    monkeypatch.setattr("codey.control.client.prepare", lambda service, args: {
        "release_id": current["release_id"], "candidate_digest": current["candidate_digest"]})
    monkeypatch.setattr("codey.control.client.activate", lambda service, args: {
        "upgrade_id": "upgrade_" + "c" * 32, "phase": "accepted"})

    def call(self, operation, params=None):
        calls.append((operation, params))
        return next(responses)

    monkeypatch.setattr(ControlClient, "call", call)
    ensure_service(tmp_path)
    assert [item[0] for item in calls] == ["get_health", "get_upgrade", "get_health"]


def test_ensure_service_retries_interrupted_validation_with_fresh_request(tmp_path, monkeypatch):
    current = {"release_id": "release_" + "b" * 32, "candidate_digest": "b" * 64,
               "source": str(tmp_path / "releases" / ("release_" + "b" * 32) / "source")}
    prepare_calls = []
    responses = iter([
        {"release_id": "release_" + "a" * 32, "instance_id": "old-instance", "status": "ready"},
        {"phase": "active"},
        {"release_id": current["release_id"], "status": "ready"},
    ])

    monkeypatch.setattr("codey.control.client._materialize_current_release", lambda home: current)
    monkeypatch.setattr("codey.control.client._current_source", lambda: Path.cwd())

    def prepare(service, args):
        prepare_calls.append(dict(args))
        if len(prepare_calls) == 1:
            raise ControlError("OUTCOME_UNKNOWN", "interrupted")
        return current

    monkeypatch.setattr("codey.control.client.prepare", prepare)
    monkeypatch.setattr("codey.control.client.activate", lambda service, args: {
        "upgrade_id": "upgrade_" + "c" * 32, "phase": "accepted"})
    monkeypatch.setattr(ControlClient, "call", lambda self, operation, params=None: next(responses))

    ensure_service(tmp_path)
    assert len(prepare_calls) == 2
    assert prepare_calls[1]["request_id"].startswith(prepare_calls[0]["request_id"] + "-recovery-")


def test_local_service_rejects_source_runtime(tmp_path):
    from codey.app.service import LocalService

    with pytest.raises(ValueError, match="immutable release_id"):
        LocalService(tmp_path, release_id="source").start()
