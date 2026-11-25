"""A coordinator startup failure settles the durable activation journal."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.launcher import releases
from codey.storage.local_store import write_json_atomic


@dataclass
class _Context:
    state_home: Path


@dataclass
class _Service:
    ctx: _Context
    instance_id: str = "active-instance"
    allowed_roots: tuple[Path, ...] = ()


def test_activation_records_failed_coordinator_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    digest = "a" * 64
    release_id = "release_candidate"
    write_json_atomic(releases.release_path(tmp_path, release_id) / "manifest.json", {
        "release_id": release_id, "candidate_digest": digest, "source": str(source), "created_at": 1.0,
        "status": "validated", "validation_source": str(source), "static_validation_key": "environment",
        "verification": [],
    })
    def runtime_digest(_path: Path) -> str:
        return digest

    def validation_key(_digest: str, _commands: list[list[str]]) -> str:
        return "environment"

    monkeypatch.setattr(releases, "runtime_digest", runtime_digest)
    monkeypatch.setattr(releases, "validation_key", validation_key)
    launches: list[object] = []

    def launch(argv: list[str], **_kwargs: object) -> None:
        launches.append(argv)
        raise FileNotFoundError("coordinator unavailable")

    monkeypatch.setattr(subprocess, "Popen", launch)
    service = _Service(_Context(tmp_path))
    args: releases.ActivateArgs = {
        "request_id": "attempt", "candidate_release": release_id,
        "candidate_digest": digest, "expected_instance_id": service.instance_id,
    }

    result = releases.activate(service, args)

    assert result["phase"] == "aborted"
    journal = releases.read_upgrade(tmp_path, result["upgrade_id"])
    assert journal["phase"] == "aborted"
    assert journal["error"] == "Release coordinator could not be started"
    assert journal["admission_may_have_opened"] is False
    assert releases.activate(service, args) == result
    assert len(launches) == 1
