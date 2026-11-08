"""Generic optional authentication must not alter settings drafts or auth scope."""
import json
import threading
from types import SimpleNamespace

from codey.app import connection_actions
from codey.app.connection_status import ConnectionStatusCoordinator
from codey.providers.connection_controls import ConnectionActionError
from codey.providers.model_preferences import ModelPreferences


def context(tmp_path):
    providers = SimpleNamespace(model_preferences=ModelPreferences(tmp_path), model_uses=[])
    providers.model_uses_snapshot = lambda: tuple(providers.model_uses)
    return SimpleNamespace(lock=threading.RLock(), active_runs=lambda: (),
        approvals=SimpleNamespace(shell_snapshot=lambda: {}),
        providers=providers)


def connector():
    auth = {"kind": "oauth_device", "state": "signed_out", "revision": 1, "actions": ["login"]}
    return SimpleNamespace(authentication_payload=lambda: auth,
        cached_model_payload=lambda: {"id": "fixture", "state": "unavailable", "models": [{"id": "m"}]},
        connection_action=lambda *_: {**auth, "state": "starting"})


def test_controls_are_optional_and_safe(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(connection_actions, "connection_for", lambda _: SimpleNamespace())
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "login"})[0] == 400
    for body in (None, {}, {"source": "fixture", "action": []}, {"source": "fixture", "action": "nope"}):
        assert connection_actions.action_response(ctx, body)[0] == 400
    monkeypatch.setattr(connection_actions, "connection_for", lambda _: connector())
    before = ctx.providers.model_preferences.snapshot()
    status, body = connection_actions.action_response(ctx, {"source": "fixture", "action": "login"})
    assert status == 202 and body["authentication"]["state"] == "starting"
    assert ctx.providers.model_preferences.snapshot() == before
    assert connection_actions.authentication_response(ctx, {"source": ["fixture"]})[0] == 200


def test_logout_and_final_admission_share_the_app_lock(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    module = connector()
    calls = []
    module.connection_action = lambda *_: calls.append(ctx.lock._is_owned()) or {"state": "signed_out"}
    monkeypatch.setattr(connection_actions, "connection_for", lambda _: module)
    ctx.providers.model_uses = [("fixture", "m")]
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "logout"})[0] == 409
    assert calls == []
    ctx.providers.model_uses = []
    assert connection_actions.action_response(ctx, {"source": "fixture", "action": "logout"})[0] == 200
    assert calls == [True]


def test_third_party_exception_is_never_returned(tmp_path, monkeypatch):
    module = connector()
    module.connection_action = lambda *_: (_ for _ in ()).throw(RuntimeError("SECRET_MARKER"))
    monkeypatch.setattr(connection_actions, "connection_for", lambda _: module)
    status, body = connection_actions.action_response(context(tmp_path), {"source": "fixture", "action": "login"})
    assert status == 503 and "SECRET_MARKER" not in json.dumps(body)


def test_explicit_catalog_state_overrides_nonempty_models():
    coordinator = ConnectionStatusCoordinator(provider_ids=lambda: ["fixture"], labels=lambda: {"fixture": "Fixture"},
        enabled=lambda _: True, connector=lambda _: SimpleNamespace(status_key=lambda: "rev"))
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


def test_safe_connection_error_preserves_status(tmp_path, monkeypatch):
    module = connector()
    module.connection_action = lambda *_: (_ for _ in ()).throw(ConnectionActionError("reauth_required", "Sign in.", 401))
    monkeypatch.setattr(connection_actions, "connection_for", lambda _: module)
    assert connection_actions.action_response(context(tmp_path), {"source": "fixture", "action": "login"}) == (
        401, {"ok": False, "code": "reauth_required", "error": "Sign in."})


def test_control_shutdown_continues_after_failure_and_does_not_import_missing_packages(monkeypatch, caplog):
    import sys

    from codey.app import provider_registry

    calls = []

    def fail():
        calls.append("first")
        raise RuntimeError("SECRET_MARKER")

    monkeypatch.setattr(provider_registry, "API_CONNECTIONS", {
        "first": ("First", "test_controls.first"),
        "missing": ("Missing", "not_installed_connector.controls"),
        "last": ("Last", "test_controls.last"),
    })
    monkeypatch.setitem(sys.modules, "test_controls.first", SimpleNamespace(shutdown_controls=fail))
    monkeypatch.setitem(sys.modules, "test_controls.last", SimpleNamespace(shutdown_controls=lambda: calls.append("last")))
    registry = object.__new__(provider_registry.ProviderRegistry)
    assert registry.shutdown_controls() is False
    assert calls == ["first", "last"]
    assert "not_installed_connector.controls" not in sys.modules and "SECRET_MARKER" not in caplog.text
    monkeypatch.setattr(sys.modules["test_controls.first"], "shutdown_controls", lambda: None)
    assert registry.shutdown_controls() is True
