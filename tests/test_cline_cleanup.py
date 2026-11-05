"""Credentials and disposable state must be removed before deleting the package."""
import json
import threading
from types import SimpleNamespace

import pytest

from codey.app import connection_actions
from codey.providers.cline import connection
from codey.providers.cline.credentials import WindowsTokenStore
from codey.providers.cline.runtime import Runtime
from codey.providers.model_preferences import ModelPreferences
from tests.support.cline import MODEL, credential, resource


def test_complete_cleanup_removes_credentials_cache_and_preferences_but_not_history(tmp_path, monkeypatch):
    instance = resource(tmp_path / "connections" / "cline")
    monkeypatch.setattr(connection, "runtime", lambda: instance)
    monkeypatch.setattr(connection_actions, "connection_for", lambda _: connection)
    preferences = ModelPreferences(tmp_path)
    prefs = preferences.snapshot()
    prefs["sources"]["cline"] = {"enabled": True, "models": [MODEL]}
    preferences.save(prefs["sources"], base_revision=prefs["revision"])
    preferences.observe_catalog(instance.payload())
    history = tmp_path / "retained-history.json"
    history.write_text(json.dumps({"provider": "cline", "model": MODEL, "text": "Keep this"}), encoding="utf8")
    ctx = SimpleNamespace(lock=threading.RLock(), active_runs=lambda: (),
        approvals=SimpleNamespace(shell_snapshot=lambda: {}),
        providers=SimpleNamespace(model_preferences=preferences, model_uses=[]))
    status, payload = connection_actions.action_response(ctx, {"source": "cline", "action": "clear_data"})
    assert status == 200 and payload["authentication"]["state"] == "signed_out"
    assert instance.auth.store.load() is None and not instance.catalog.path.exists()
    assert preferences.snapshot()["sources"]["cline"] == {"enabled": False, "models": []}
    assert preferences.catalog("cline")["models"] == []
    assert json.loads(history.read_text())["text"] == "Keep this"
    assert "MARKER" not in json.dumps(payload)


def test_logout_preserves_discovery_and_active_request_prevents_all_cleanup(tmp_path):
    instance = resource(tmp_path)
    instance.active = 1
    with pytest.raises(RuntimeError, match="Stop tasks"):
        instance.action("clear_data", None)
    assert instance.auth.store.load() and instance.catalog.path.exists()
    instance.active = 0
    instance.action("logout", None)
    assert instance.auth.store.load() is None and instance.catalog.path.exists()


def test_failed_cleanup_reports_remaining_items_without_claiming_success(tmp_path, monkeypatch):
    instance = resource(tmp_path)

    def fail():
        raise OSError("SENSITIVE_PATH")

    monkeypatch.setattr(instance.auth.store, "delete", fail)
    with pytest.raises(RuntimeError) as error:
        instance.action("clear_data", None)
    assert error.value.pending_cleanup == ("system_credentials",)
    assert "SENSITIVE_PATH" not in str(error.value)
    assert not instance.catalog.path.exists()


@pytest.mark.parametrize("action", ["logout", "clear_data"])
def test_cost_block_survives_restart_and_requires_explicit_cleanup(tmp_path, action):
    instance = resource(tmp_path)
    instance.block_unexpected_cost()
    instance.close()
    restarted = Runtime(tmp_path, store=instance.auth.store, http=instance.catalog.http)
    try:
        assert restarted.blocked and restarted.payload()["state"] == "blocked"
        assert restarted.auth.snapshot()["state"] == "ready"
        restarted.action(action, None)
        assert not restarted.blocked and not restarted.block_path.exists()
        assert restarted.auth.store.load() is None
        assert restarted.catalog.path.exists() is (action == "logout")
    finally:
        restarted.close()


def test_corrupt_cost_block_fails_closed_and_cleanup_reports_cache_failure(tmp_path, monkeypatch):
    instance = resource(tmp_path)
    instance.block_path.write_text("broken", encoding="utf8")
    restarted = Runtime(tmp_path, store=instance.auth.store, http=instance.catalog.http)
    assert restarted.blocked

    def fail():
        raise OSError("PRIVATE_CACHE_PATH")

    monkeypatch.setattr(restarted.catalog, "clear", fail)
    with pytest.raises(RuntimeError) as error:
        restarted.action("clear_data", None)
    assert error.value.pending_cleanup == ("model_catalog_cache",)
    assert "PRIVATE_CACHE_PATH" not in str(error.value)
    assert restarted.auth.store.load() is None and restarted.blocked


def test_clear_while_public_directory_refreshes_rejects_late_repopulation(tmp_path):
    instance = resource(tmp_path)
    arrived, release = threading.Event(), threading.Event()

    def hook(url, *_):
        if url.endswith("recommended-models"):
            arrived.set()
            assert release.wait(3)
        return None

    instance.catalog.http.hook = hook
    thread = threading.Thread(target=lambda: instance.catalog.refresh(force=True))
    thread.start()
    assert arrived.wait(2)
    instance.action("clear_data", None)
    release.set()
    thread.join(3)
    assert not thread.is_alive() and not instance.catalog.path.exists()
    assert not instance.payload()["models"]


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows Credential Manager roundtrip")
def test_real_windows_credential_store_roundtrip_and_delete_uses_isolated_target():
    from uuid import uuid4

    store = WindowsTokenStore("Codey/Cline/test-" + uuid4().hex)
    try:
        assert store.load() is None
        store.save(credential())
        assert store.load().access_token == "ACCESS_MARKER"
        store.delete()
        assert store.load() is None
        store.delete()
    finally:
        store.delete()
