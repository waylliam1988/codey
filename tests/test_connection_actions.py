"""Optional connection actions preserve admission, privacy and shutdown contracts."""
from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

from codey.app import connection_actions
from codey.app.connection_status import ConnectionStatusCoordinator
from codey.app.context import AppContext
from codey.providers.connection_controls import ConnectionActionError
from codey.types.json_value import JsonObject


@pytest.fixture()
def ctx(tmp_path: Path) -> Iterator[AppContext]:
    app = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        yield app
    finally:
        app.close()


class Connector(ModuleType):
    def authentication_payload(self) -> JsonObject:
        return {"kind": "oauth_device", "state": "signed_out", "revision": 1, "actions": ["login"]}

    def cached_model_payload(self) -> JsonObject:
        return {"id": "fixture", "state": "unavailable", "models": [{"id": "m"}]}

    def connection_action(self, action: str, attempt: str | None) -> JsonObject:
        return {**self.authentication_payload(), "state": "starting"}

    def status_key(self) -> str:
        return "rev"


def install_connector(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
    def connection(source: str) -> ModuleType:
        return module

    monkeypatch.setattr(connection_actions, "connection_for", connection)


def test_controls_are_optional_and_safe(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    install_connector(monkeypatch, ModuleType("fixture"))
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "login"})[0] == 400
    invalid_bodies: list[JsonObject | None] = [
        None, {}, {"source": "fixture", "action": []}, {"source": "fixture", "action": "nope"},
    ]
    for body in invalid_bodies:
        assert connection_actions.action_response(ctx, body)[0] == 400
    install_connector(monkeypatch, Connector("fixture"))
    before: object = ctx.providers.model_preferences.snapshot()
    status, payload = connection_actions.action_response(ctx, {"source": "fixture", "action": "login"})
    authentication = payload["authentication"]
    assert isinstance(authentication, dict)
    assert status == 202 and authentication["state"] == "starting"
    after: object = ctx.providers.model_preferences.snapshot()
    assert after == before
    assert connection_actions.authentication_response(ctx, {"source": ["fixture"]})[0] == 200


def test_logout_and_final_admission_share_the_app_lock(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    module = Connector("fixture")
    calls: list[bool] = []

    def logout(action: str, attempt: str | None) -> JsonObject:
        def observe_lock() -> None:
            acquired = ctx.lock.acquire(blocking=False)
            calls.append(not acquired)
            if acquired:
                ctx.lock.release()

        observer = threading.Thread(target=observe_lock)
        observer.start()
        observer.join(timeout=2.0)
        assert not observer.is_alive()
        return {"state": "signed_out"}

    monkeypatch.setattr(module, "connection_action", logout)
    install_connector(monkeypatch, module)
    ctx.providers.model_uses[("fixture", "m")] = 1
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "logout"})[0] == 409
    assert calls == []
    ctx.providers.model_uses.clear()
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "logout"})[0] == 200
    assert calls == [True]


def test_third_party_exception_is_never_returned(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    module = Connector("fixture")

    def fail(action: str, attempt: str | None) -> JsonObject:
        raise RuntimeError("SECRET_MARKER")

    monkeypatch.setattr(module, "connection_action", fail)
    install_connector(monkeypatch, module)
    status, body = connection_actions.action_response(ctx, {"source": "fixture", "action": "login"})
    assert status == 503 and "SECRET_MARKER" not in json.dumps(body)


def test_explicit_catalog_state_overrides_nonempty_models() -> None:
    coordinator = ConnectionStatusCoordinator(provider_ids=lambda: ["fixture"], labels=lambda: {"fixture": "Fixture"},
        enabled=lambda _: True, connector=lambda _: Connector("fixture"))
    try:
        coordinator.observe_catalog("fixture", {"models": [{"id": "m"}], "state": "unavailable"})
        assert coordinator.current("fixture").state == "unavailable"
        coordinator.observe_catalog("fixture", {"models": [{"id": "m"}]})
        assert coordinator.current("fixture").state == "ready"
        coordinator.observe_catalog("fixture", {"models": [{"id": "m"}], "state": "blocked"})
        assert coordinator.current("fixture").state == "blocked"
        assert not coordinator._last_good
    finally:
        coordinator.close()


def test_safe_connection_error_preserves_status(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    module = Connector("fixture")

    def fail(action: str, attempt: str | None) -> JsonObject:
        raise ConnectionActionError("reauth_required", "Sign in.", 401)

    monkeypatch.setattr(module, "connection_action", fail)
    install_connector(monkeypatch, module)
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "login"}) == (
        401, {"ok": False, "code": "reauth_required", "error": "Sign in."})


class ControlModule(ModuleType):
    def __init__(self, name: str, shutdown: Callable[[], None]) -> None:
        super().__init__(name)
        self.shutdown_controls = shutdown


def test_control_shutdown_continues_after_failure_and_does_not_import_missing_packages(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from codey.app import provider_registry

    calls: list[str] = []

    def fail() -> None:
        calls.append("first")
        raise RuntimeError("SECRET_MARKER")

    def last() -> None:
        calls.append("last")

    def closed() -> None:
        pass

    monkeypatch.setattr(provider_registry, "API_CONNECTIONS", {
        "first": ("First", "test_controls.first"),
        "missing": ("Missing", "not_installed_connector.controls"),
        "last": ("Last", "test_controls.last"),
    })
    first_module = ControlModule("test_controls.first", fail)
    monkeypatch.setitem(sys.modules, "test_controls.first", first_module)
    monkeypatch.setitem(sys.modules, "test_controls.last", ControlModule("test_controls.last", last))
    registry = object.__new__(provider_registry.ProviderRegistry)
    assert registry.shutdown_controls() is False
    assert calls == ["first", "last"]
    assert "not_installed_connector.controls" not in sys.modules and "SECRET_MARKER" not in caplog.text
    monkeypatch.setattr(first_module, "shutdown_controls", closed)
    assert registry.shutdown_controls() is True
