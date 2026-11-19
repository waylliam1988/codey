"""Discovery publication has one writer across startup and scheduler threads."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from codey.app import service


def test_service_publications_serialize_snapshot_and_atomic_replace(tmp_path, monkeypatch):
    runtime = service.LocalService(tmp_path, release_id="immutable")
    runtime.control = SimpleNamespace(instance_id="instance", started_at="now", health=lambda: {"status": "active"})
    first_entered, release_first, second_started, second_entered = (Event() for _ in range(4))
    write = service.write_json_atomic
    snapshots = []

    def blocked_write(path, value, **kwargs):
        snapshots.append(value)
        if len(snapshots) == 1:
            first_entered.set()
            assert release_first.wait(5)
        else:
            second_entered.set()
        write(path, value, **kwargs)

    def publish_second():
        second_started.set()
        runtime._publish()

    monkeypatch.setattr(service, "write_json_atomic", blocked_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime._publish)
        try:
            assert first_entered.wait(5)
            runtime.control.health = lambda: {"status": "draining"}
            second = pool.submit(publish_second)
            assert second_started.wait(5)
            assert not second_entered.wait(0.1), "concurrent discovery replacement"
        finally:
            release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert [snapshot["status"] for snapshot in snapshots] == ["active", "draining"]
    assert service.read_json_strict(tmp_path / "service.json")["status"] == "draining"


def test_service_publication_surfaces_io_failure_and_releases_writer(tmp_path, monkeypatch):
    runtime = service.LocalService(tmp_path, release_id="immutable")
    runtime.control = SimpleNamespace(instance_id="instance", started_at="now", health=lambda: {"status": "active"})
    write = service.write_json_atomic

    def fail(*args, **kwargs):
        raise PermissionError("discovery access denied")

    monkeypatch.setattr(service, "write_json_atomic", fail)
    with pytest.raises(PermissionError, match="access denied"):
        runtime._publish()
    monkeypatch.setattr(service, "write_json_atomic", write)
    runtime._publish()
    assert service.read_json_strict(tmp_path / "service.json")["instance_id"] == "instance"


@pytest.mark.parametrize("permanent", [False, True])
def test_discovery_replacement_bounds_windows_access_denial_retry(tmp_path, monkeypatch, permanent):
    runtime = service.LocalService(tmp_path, release_id="immutable")
    runtime.control = SimpleNamespace(instance_id="instance", started_at="now", health=lambda: {"status": "active"})
    write = service.write_json_atomic
    calls = []

    def access_denied(path, value, **kwargs):
        calls.append(value)
        if permanent or len(calls) == 1:
            error = PermissionError("discovery is open in coordinator")
            error.winerror = 5
            raise error
        write(path, value, **kwargs)

    monkeypatch.setattr(service, "write_json_atomic", access_denied)
    if permanent:
        with pytest.raises(PermissionError, match="coordinator"):
            runtime._publish()
        assert 1 < len(calls) <= 8
        assert not (tmp_path / "service.json").exists()
    else:
        runtime._publish()
        assert len(calls) == 2
        assert service.read_json_strict(tmp_path / "service.json")["instance_id"] == "instance"
