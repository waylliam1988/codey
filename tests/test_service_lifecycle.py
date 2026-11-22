"""Service ownership ends only after cleanup, independently of discovery I/O."""

from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.service import LocalService, credentials
from codey.control.protocol import Actor, ControlError
from codey.storage.file_lock import LockTimeout, acquire_lease
from codey.storage.local_store import StoreCorruption, read_json_strict, write_json_atomic
from codey.utils.object_fields import object_fields


def assert_ownership_released(runtime: LocalService) -> None:
    assert runtime.lease is None, "finished service retained its writer lease"


def assert_resources_closed(runtime: LocalService) -> None:
    assert runtime.ctx is None and runtime.httpd is None
    assert_ownership_released(runtime)


def test_credentials_round_trip_preserves_existing_fields(tmp_path: Path) -> None:
    initial = credentials(tmp_path)
    assert len(initial["operator"]) >= 32 and len(initial["agent"]) >= 32
    assert initial["operator"] != initial["agent"]
    assert credentials(tmp_path) == initial
    saved: dict[str, object] = {
        "operator": initial["operator"], "agent": initial["agent"], "extension": {"version": 1},
    }
    write_json_atomic(tmp_path / "control-credentials.json", saved)
    loaded: object = credentials(tmp_path)
    assert object_fields(loaded) and loaded == saved


@pytest.mark.parametrize("token", [None, 42, "short", "x" * 31, [], {}])
def test_credentials_reject_invalid_tokens_without_overwriting(tmp_path: Path, token: object) -> None:
    path = tmp_path / "control-credentials.json"
    write_json_atomic(path, {"operator": token, "agent": "a" * 32})
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid local control credentials"):
        credentials(tmp_path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("release_id", ["", "source"])
def test_rejected_startup_releases_ownership(tmp_path: Path, release_id: str) -> None:
    runtime = LocalService(tmp_path, release_id=release_id)
    with pytest.raises(ValueError, match="immutable release_id"):
        runtime.start()
    assert_resources_closed(runtime)
    assert not (tmp_path / "service.json").exists()
    with acquire_lease(tmp_path / ".server.lock"):
        pass


@pytest.mark.parametrize("failure", ["corrupt", "unlink"])
def test_close_releases_ownership_after_discovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    runtime = LocalService(tmp_path, release_id="immutable")
    runtime.instance_id = "owner"
    runtime.lease = acquire_lease(tmp_path / ".server.lock")
    discovery = tmp_path / "service.json"
    error: type[Exception]
    if failure == "corrupt":
        discovery.write_text("{broken", encoding="utf-8")
        error = StoreCorruption
    else:
        write_json_atomic(discovery, {"instance_id": "owner"})
        error = PermissionError
        unlink = Path.unlink

        def denied(path: Path, missing_ok: bool = False) -> None:
            if path == discovery:
                raise PermissionError("discovery deletion denied")
            unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", denied)
    original = discovery.read_bytes()
    try:
        with pytest.raises(error):
            runtime.close()
        assert_ownership_released(runtime)
        assert discovery.read_bytes() == original
        with acquire_lease(tmp_path / ".server.lock"):
            pass
    finally:
        if runtime.lease is not None:
            runtime.lease.release()


@pytest.mark.parametrize("owner", [None, "owner", "successor"])
def test_close_removes_only_its_own_discovery(tmp_path: Path, owner: str | None) -> None:
    runtime = LocalService(tmp_path, release_id="immutable")
    runtime.instance_id = "owner"
    runtime.lease = acquire_lease(tmp_path / ".server.lock")
    if owner is not None:
        write_json_atomic(tmp_path / "service.json", {"instance_id": owner})
    runtime.close()
    runtime.close()
    assert_ownership_released(runtime)
    assert read_json_strict(tmp_path / "service.json") == (
        {"instance_id": "successor"} if owner == "successor" else None
    )
    with acquire_lease(tmp_path / ".server.lock"):
        pass


def test_close_keeps_ownership_while_context_cleanup_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LocalService(tmp_path, release_id="immutable")
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    runtime.ctx = ctx
    runtime.lease = acquire_lease(tmp_path / ".server.lock")
    close = ctx.close

    def pending() -> bool:
        return False

    def no_delay(_seconds: float) -> None:
        pass

    monkeypatch.setattr(ctx, "close", pending)
    monkeypatch.setattr("codey.app.service.time.sleep", no_delay)
    try:
        with pytest.raises(RuntimeError, match="ownership was not released"):
            runtime.close()
        assert runtime.lease is not None
        with pytest.raises(LockTimeout):
            acquire_lease(tmp_path / ".server.lock")
    finally:
        monkeypatch.setattr(ctx, "close", close)
        runtime.close()


def test_real_service_lifecycle_and_credentials(tmp_path: Path) -> None:
    runtime = LocalService(tmp_path, release_id="immutable-fixture", verify_only=True)
    try:
        runtime.start()
        keys = credentials(tmp_path)
        operator_token = keys["operator"]
        agent_token = keys["agent"]
        assert isinstance(operator_token, str) and isinstance(agent_token, str)
        operator = runtime.authenticate(f"Bearer {operator_token}")
        agent = runtime.authenticate(f"Bearer {agent_token}")
        assert operator is not None and operator.operator
        assert agent is not None and not agent.operator
        assert runtime.authenticate("Bearer invalid") is None
        assert runtime.authenticate("Bearer 非ASCII") is None
        assert runtime.authenticate(operator_token) is None
        params = {"expected_instance_id": runtime.instance_id}
        health = runtime.lifecycle(operator, "status", params)
        assert health["status"] == "verifying"
        assert health["quiescent"] is True
        desktop_url = runtime.lifecycle(operator, "desktop_url", params)["url"]
        assert isinstance(desktop_url, str)
        assert desktop_url.startswith(f"http://127.0.0.1:{runtime.port}/#codey_bootstrap=")
        assert operator_token not in desktop_url and agent_token not in desktop_url
        with pytest.raises(ControlError, match="user capability"):
            runtime.lifecycle(Actor("agent", "Agent"), "shutdown", params)
        with pytest.raises(ControlError, match="instance changed"):
            runtime.lifecycle(operator, "status", {"expected_instance_id": "stale"})
        with pytest.raises(ControlError, match="Invalid lifecycle parameters"):
            runtime.lifecycle(operator, "status", {**params, "extra": True})
        runtime.lifecycle(operator, "activate", params)
        assert runtime.ctx is not None
        assert runtime.ctx.durable_scheduler.health()["enabled"] is True
        with pytest.raises(ControlError, match="must drain"):
            runtime.lifecycle(operator, "shutdown", params)
        assert not runtime.stop_event.is_set()
        assert runtime.lifecycle(operator, "drain", params)["status"] == "draining"
        runtime.lifecycle(operator, "shutdown", params)
        assert runtime.stop_event.is_set()
    finally:
        runtime.close()
    assert_resources_closed(runtime)
    assert not (tmp_path / "service.json").exists()
