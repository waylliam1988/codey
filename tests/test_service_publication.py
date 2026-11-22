"""Discovery publication has one writer across startup and scheduler threads."""
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from codey.app import service
from codey.app.context import AppContext
from codey.app.control_service import ControlService
from codey.storage.atomic_io import MAX_ATOMIC_JSON_BYTES, write_json_atomic
from codey.storage.local_store import read_json_strict


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[service.LocalService]:
    instance = service.LocalService(tmp_path, release_id="immutable")
    instance.ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    instance.control = ControlService(instance.ctx, allowed_roots=(), instance_id="instance")

    def active() -> dict[str, object]:
        return {"status": "active"}

    monkeypatch.setattr(instance.control, "health", active)
    try:
        yield instance
    finally:
        instance.close()


def test_service_publications_serialize_snapshot_and_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: service.LocalService,
) -> None:
    first_entered, release_first, second_started, second_entered = (Event() for _ in range(4))
    snapshots: list[dict[str, object]] = []

    def blocked_write(
        path: str | Path, value: dict[str, object], *, mode: int | None = None,
        preserve_mode: bool = True, max_bytes: int = MAX_ATOMIC_JSON_BYTES,
    ) -> None:
        snapshots.append(value)
        if len(snapshots) == 1:
            first_entered.set()
            assert release_first.wait(5)
        else:
            second_entered.set()
        write_json_atomic(path, value, mode=mode, preserve_mode=preserve_mode, max_bytes=max_bytes)

    def publish_second() -> None:
        second_started.set()
        runtime._publish()

    def draining() -> dict[str, object]:
        return {"status": "draining"}

    monkeypatch.setattr(service, "write_json_atomic", blocked_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime._publish)
        try:
            assert first_entered.wait(5)
            assert runtime.control is not None
            monkeypatch.setattr(runtime.control, "health", draining)
            second = pool.submit(publish_second)
            assert second_started.wait(5)
            assert not second_entered.wait(0.1), "concurrent discovery replacement"
        finally:
            release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert [snapshot["status"] for snapshot in snapshots] == ["active", "draining"]
    record = read_json_strict(tmp_path / "service.json")
    assert record is not None and record["status"] == "draining"


def test_service_publication_surfaces_io_failure_and_releases_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: service.LocalService,
) -> None:
    def fail(
        path: str | Path, value: dict[str, object], *, mode: int | None = None,
        preserve_mode: bool = True, max_bytes: int = MAX_ATOMIC_JSON_BYTES,
    ) -> None:
        raise PermissionError("discovery access denied")

    monkeypatch.setattr(service, "write_json_atomic", fail)
    with pytest.raises(PermissionError, match="access denied"):
        runtime._publish()
    monkeypatch.setattr(service, "write_json_atomic", write_json_atomic)
    runtime._publish()
    record = read_json_strict(tmp_path / "service.json")
    assert record is not None and record["instance_id"] == "instance"


class _WindowsAccessDenied(PermissionError):
    winerror = 5


@pytest.mark.parametrize("permanent", [False, True])
def test_discovery_replacement_bounds_windows_access_denial_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, permanent: bool, runtime: service.LocalService,
) -> None:
    calls: list[dict[str, object]] = []

    def access_denied(
        path: str | Path, value: dict[str, object], *, mode: int | None = None,
        preserve_mode: bool = True, max_bytes: int = MAX_ATOMIC_JSON_BYTES,
    ) -> None:
        calls.append(value)
        if permanent or len(calls) == 1:
            raise _WindowsAccessDenied("discovery is open in coordinator")
        write_json_atomic(path, value, mode=mode, preserve_mode=preserve_mode, max_bytes=max_bytes)

    monkeypatch.setattr(service, "write_json_atomic", access_denied)
    if permanent:
        with pytest.raises(PermissionError, match="coordinator"):
            runtime._publish()
        assert 1 < len(calls) <= 8
        assert not (tmp_path / "service.json").exists()
    else:
        runtime._publish()
        assert len(calls) == 2
        record = read_json_strict(tmp_path / "service.json")
        assert record is not None and record["instance_id"] == "instance"
