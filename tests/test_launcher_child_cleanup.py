"""The coordinator reaps its own child after a startup rejection."""

import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.launcher import runner


@dataclass
class _Child:
    terminated: bool = False
    reaped: bool = False
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, *, timeout: int) -> int:
        assert self.terminated and timeout == 10
        self.reaped = True
        self.returncode = 1
        return 1


def test_unexpected_release_health_reaps_started_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = _Child()

    def start(_argv: list[str], **_kwargs: object) -> _Child:
        return child

    def request(_home: Path, operation: str, *, lifecycle: bool) -> dict[str, object]:
        assert operation == "get_health" and not lifecycle
        return {"release_id": "old", "instance_id": "old-instance", "status": "ready"}

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(runner, "request", request)
    journal: dict[str, object] = {
        "python": sys.executable, "upgrade_id": "upgrade_test", "allowed_roots": [],
        "old_instance_id": "old-instance",
    }
    release: dict[str, object] = {"release_id": "candidate", "source": str(tmp_path)}

    with pytest.raises(RuntimeError, match="Unexpected release instance"):
        runner.launch(tmp_path, journal, release, time.time() + 10)

    assert child.terminated and child.reaped


def test_failed_candidate_rolls_back_using_the_current_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = tmp_path / "service.json"
    runner.write(service, {"instance_id": "old-instance", "url": "http://127.0.0.1:3000"})
    previous = {"release_id": "old", "source": str(tmp_path)}
    candidate = {"release_id": "candidate", "source": str(tmp_path)}
    journal: dict[str, object] = {
        "upgrade_id": "upgrade_test", "old_instance_id": "old-instance", "phase": "accepted",
        "deadline": time.time() + 10, "events": [], "attempts": 0, "candidate": candidate, "previous": previous,
    }
    operations: list[tuple[str, str]] = []

    def request(_home: Path, operation: str, instance: str) -> dict[str, object]:
        operations.append((operation, instance))
        if operation == "shutdown":
            service.unlink()
        return {"quiescent": True}

    def launch(_home: Path, _journal: Mapping[str, object], release: Mapping[str, object], _deadline: float) -> str:
        if release == candidate:
            raise RuntimeError("candidate failed")
        assert release == previous
        return "restored-instance"

    monkeypatch.setattr(runner, "request", request)
    monkeypatch.setattr(runner, "launch", launch)
    runner.coordinate(tmp_path, journal)

    assert journal["phase"] == "rolled_back"
    assert journal["schema_digest"] == runner.schema(tmp_path)
    assert runner.read(tmp_path / "active-release.json") == previous
    assert operations == [("drain", "old-instance"), ("status", "old-instance"),
                          ("shutdown", "old-instance"), ("activate", "restored-instance")]
