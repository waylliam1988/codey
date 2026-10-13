"""Cold-start provider status contracts, including removable API connectors."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from codey.app import api
from codey.app.context import AppContext
from codey.providers import catalog, local_connection
from codey.providers.zen import connection as zen_connection


def _enable(ctx, *providers: str) -> None:
    view = ctx.providers.model_preferences.snapshot()
    view["sources"]["websites"] = {"enabled": False, "models": []}
    for provider in providers:
        view["sources"][provider] = {"enabled": True, "models": ["model"]}
    ctx.providers.model_preferences.save(view["sources"], base_revision=view["revision"])


def test_provider_snapshot_returns_checking_before_slow_zen_probe(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def probe(_request):
        entered.set()
        assert release.wait(5)
        return {"state": "ready", "source": "api_catalog", "models": [{"id": "free"}]}

    monkeypatch.setattr("codey.providers.api_connections.connection_for", lambda key: SimpleNamespace(
        status_key=lambda: "catalog-v1", probe_status=probe,
    ) if key == "zen" else SimpleNamespace(status_key=lambda: "local-v1", probe_status=lambda _: {
        "state": "unavailable", "source": "local_preflight", "models": [],
    }))
    ctx = AppContext(tmp_path)
    _enable(ctx, "zen", "local")
    try:
        status, payload = api.providers_response(ctx)
        assert status == 200
        zen = next(row for row in payload["providers"] if row["id"] == "zen")
        assert zen["state"] == "checking"
        assert entered.wait(5)
        release.set()
        assert ctx.connection_status.wait_for("zen", timeout=5)
        _, payload = api.providers_response(ctx)
        zen = next(row for row in payload["providers"] if row["id"] == "zen")
        assert (zen["state"], zen["freshness"], zen["source"]) == ("ready", "fresh", "api_catalog")
    finally:
        release.set()
        ctx.close()


def test_provider_snapshot_does_not_wait_for_slow_web_probe(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def web_probe(**_kwargs):
        entered.set()
        assert release.wait(5)
        return {"deepseek": True}

    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", web_probe)
    ctx = AppContext(tmp_path)
    _enable(ctx, "zen")
    view = ctx.providers.model_preferences.snapshot()
    view["sources"]["websites"] = {"enabled": True, "models": ["deepseek"]}
    ctx.providers.model_preferences.save(view["sources"], base_revision=view["revision"])
    try:
        started = time.perf_counter()
        status, payload = api.providers_response(ctx)
        elapsed = time.perf_counter() - started
        assert status == 200
        assert elapsed < 0.5
        deepseek = next(row for row in payload["providers"] if row["id"] == "deepseek")
        assert deepseek["state"] == "checking"
        assert entered.wait(5)
    finally:
        release.set()
        ctx.close()


def test_local_status_and_catalog_share_one_probe(tmp_path, monkeypatch):
    probe = Mock(return_value={"state": "ready", "source": "local_preflight", "models": [{"id": "model"}],
                               "local": {"connected": True, "model": "model", "models": ["model"]}})
    monkeypatch.setattr("codey.providers.api_connections.connection_for", lambda key: SimpleNamespace(
        status_key=lambda: "local-v1", probe_status=probe,
    ) if key == "local" else SimpleNamespace(status_key=lambda: "zen-v1", probe_status=lambda _: {
        "state": "unavailable", "source": "api_catalog", "models": [],
    }))
    ctx = AppContext(tmp_path)
    _enable(ctx, "local", "zen")
    try:
        api.providers_response(ctx)
        assert ctx.connection_status.wait_for("local", timeout=5)
        api.local_provider_response(ctx)
        api.api_models_response(ctx)
        assert probe.call_count == 1
    finally:
        ctx.close()


def test_changed_connection_discards_old_probe_result(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    revision = ["old"]

    def probe(request):
        if request.connection_revision == "old":
            entered.set()
            assert release.wait(5)
            return {"state": "ready", "source": "local_preflight", "models": [{"id": "old"}]}
        return {"state": "unavailable", "source": "local_preflight", "models": []}

    monkeypatch.setattr("codey.providers.api_connections.connection_for", lambda key: SimpleNamespace(
        status_key=lambda: revision[0], probe_status=probe,
    ) if key == "local" else SimpleNamespace(status_key=lambda: "zen", probe_status=lambda _: {
        "state": "unavailable", "source": "api_catalog", "models": [],
    }))
    ctx = AppContext(tmp_path)
    _enable(ctx, "local", "zen")
    try:
        api.providers_response(ctx)
        assert entered.wait(5)
        revision[0] = "new"
        ctx.connection_status.refresh("local")
        assert ctx.connection_status.wait_for("local", timeout=5)
        release.set()
        _, payload = api.providers_response(ctx)
        local = next(row for row in payload["providers"] if row["id"] == "local")
        assert local["state"] == "unavailable"
    finally:
        release.set()
        ctx.close()


def test_removed_zen_is_not_scheduled_or_imported(tmp_path, monkeypatch):
    monkeypatch.delitem(catalog.API_CONNECTIONS, "zen")
    monkeypatch.delitem(catalog.PROVIDER_LABELS, "zen")
    connection_for = Mock(side_effect=AssertionError("no API connector import"))
    monkeypatch.setattr("codey.providers.api_connections.connection_for", connection_for)
    ctx = AppContext(tmp_path)
    try:
        _, payload = api.providers_response(ctx)
        assert all(row["id"] != "zen" for row in payload["providers"])
        assert all(call.args[0] != "zen" for call in connection_for.call_args_list)
    finally:
        ctx.close()


def test_failed_refresh_preserves_last_ready_result_as_stale(tmp_path, monkeypatch):
    result = [{"state": "ready", "source": "local_preflight", "models": [{"id": "old"}]}]
    connector = SimpleNamespace(status_key=lambda: "same", probe_status=lambda _: result[0])
    monkeypatch.setattr("codey.providers.api_connections.connection_for", lambda _: connector)
    ctx = AppContext(tmp_path)
    _enable(ctx, "local")
    try:
        ctx.connection_status.refresh("local")
        assert ctx.connection_status.wait_for("local", timeout=5).state == "ready"
        result[0] = {"state": "unavailable", "source": "local_preflight", "error": "timeout", "models": []}
        ctx.connection_status.refresh("local", force=True)
        snapshot = ctx.connection_status.wait_for("local", timeout=5)
        assert (snapshot.state, snapshot.freshness, snapshot.error) == ("ready", "stale", "timeout")
        assert ctx.connection_status.data("local")["models"] == [{"id": "old"}]
    finally:
        ctx.close()


def test_refresh_keeps_last_ready_status_visible_while_probe_runs(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = [0]

    def probe(_request):
        calls[0] += 1
        if calls[0] == 2:
            entered.set()
            assert release.wait(5)
        return {"state": "ready", "source": "local_preflight", "models": [{"id": "model"}]}

    monkeypatch.setattr("codey.providers.api_connections.connection_for", lambda _: SimpleNamespace(
        status_key=lambda: "same", probe_status=probe,
    ))
    ctx = AppContext(tmp_path)
    _enable(ctx, "local")
    try:
        ctx.connection_status.refresh("local")
        assert ctx.connection_status.wait_for("local", timeout=5).state == "ready"
        during = ctx.connection_status.refresh("local", force=True)
        assert entered.wait(5)
        assert (during.state, during.freshness, during.available) == ("ready", "stale", True)
    finally:
        release.set()
        ctx.close()


def test_local_probe_reports_bootstrap_result(monkeypatch):
    local = {"connected": True, "model": "fast", "models": ["fast"], "base_url": "http://localhost:9999"}
    monkeypatch.setattr(local_connection._local_config, "local_bootstrap_payload", lambda **_: local)
    monkeypatch.setattr(local_connection, "_model_payload_from_bootstrap", lambda value: {
        "id": "local", "models": [{"id": "fast"}], "local": value,
    })
    result = local_connection.probe_status(None)
    assert result["state"] == "ready"
    assert result["source"] == "local_preflight"
    assert result["local"] is local


def test_zen_probe_reports_catalog_readiness_not_request_eligibility(monkeypatch):
    monkeypatch.setattr(zen_connection, "model_payload", lambda: {
        "models": [{"id": "free"}], "stale": False, "error": "",
    })
    result = zen_connection.probe_status(None)
    assert (result["state"], result["source"]) == ("ready", "api_catalog")
    assert "request_eligible" not in result
