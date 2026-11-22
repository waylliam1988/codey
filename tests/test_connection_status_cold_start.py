"""Cold-start provider status contracts, including removable API connectors."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.app import api
from codey.app.connection_status import ProbeRequest
from codey.app.context import AppContext
from codey.providers import catalog, local_config, local_connection
from codey.providers.zen import connection as zen_connection
from codey.utils.object_fields import object_fields


@dataclass
class _Connector:
    revision: Callable[[], str]
    probe: Callable[[ProbeRequest], dict[str, object]]

    def status_key(self) -> str:
        return self.revision()

    def probe_status(self, request: ProbeRequest) -> dict[str, object]:
        return self.probe(request)


def _install_connector(monkeypatch: pytest.MonkeyPatch, factory: Callable[[str], _Connector]) -> None:
    monkeypatch.setattr("codey.providers.api_connections.connection_for", factory)


def _provider_rows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw = payload["providers"]
    assert isinstance(raw, list)
    items: list[object] = raw
    rows: list[dict[str, object]] = []
    for item in items:
        assert object_fields(item)
        rows.append(item)
    return rows


def _enable(ctx: AppContext, *providers: str) -> None:
    view: object = ctx.providers.model_preferences.snapshot()
    assert object_fields(view)
    sources = view["sources"]
    revision = view["revision"]
    assert object_fields(sources) and type(revision) is int
    sources["websites"] = {"enabled": False, "models": list[str]()}
    for provider in providers:
        sources[provider] = {"enabled": True, "models": ["model"]}
    ctx.providers.model_preferences.save(sources, base_revision=revision)


@contextmanager
def _without_zen_registration() -> Generator[None, None, None]:
    connections = dict(catalog.API_CONNECTIONS)
    labels = dict(catalog.PROVIDER_LABELS)
    try:
        catalog.API_CONNECTIONS.pop("zen")
        catalog.PROVIDER_LABELS.pop("zen")
        yield
    finally:
        catalog.API_CONNECTIONS.clear()
        catalog.API_CONNECTIONS.update(connections)
        catalog.PROVIDER_LABELS.clear()
        catalog.PROVIDER_LABELS.update(labels)


def test_provider_snapshot_returns_checking_before_slow_zen_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def probe(_request: ProbeRequest) -> dict[str, object]:
        entered.set()
        assert release.wait(5)
        return {"state": "ready", "source": "api_catalog", "models": [{"id": "free"}]}

    _install_connector(monkeypatch, lambda key: _Connector(
        revision=lambda: "catalog-v1", probe=probe,
    ) if key == "zen" else _Connector(revision=lambda: "local-v1", probe=lambda _: {
        "state": "unavailable", "source": "local_preflight", "models": [],
    }))
    ctx = AppContext(tmp_path)
    _enable(ctx, "zen", "local")
    try:
        status, payload = api.providers_response(ctx)
        assert status == 200
        zen = next(row for row in _provider_rows(payload) if row["id"] == "zen")
        assert zen["state"] == "checking"
        assert entered.wait(5)
        release.set()
        assert ctx.connection_status.wait_for("zen", timeout=5)
        _, payload = api.providers_response(ctx)
        zen = next(row for row in _provider_rows(payload) if row["id"] == "zen")
        assert (zen["state"], zen["freshness"], zen["source"]) == ("ready", "fresh", "api_catalog")
    finally:
        release.set()
        ctx.close()


def test_provider_snapshot_does_not_wait_for_slow_web_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def web_probe(**_kwargs: object) -> dict[str, bool]:
        entered.set()
        assert release.wait(5)
        return {"deepseek": True}

    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", web_probe)
    ctx = AppContext(tmp_path)
    _enable(ctx, "zen")
    view: object = ctx.providers.model_preferences.snapshot()
    assert object_fields(view)
    sources = view["sources"]
    revision = view["revision"]
    assert object_fields(sources) and type(revision) is int
    sources["websites"] = {"enabled": True, "models": ["deepseek"]}
    ctx.providers.model_preferences.save(sources, base_revision=revision)
    try:
        started = time.perf_counter()
        status, payload = api.providers_response(ctx)
        elapsed = time.perf_counter() - started
        assert status == 200
        assert elapsed < 0.5
        deepseek = next(row for row in _provider_rows(payload) if row["id"] == "deepseek")
        assert deepseek["state"] == "checking"
        assert entered.wait(5)
    finally:
        release.set()
        ctx.close()


def test_local_status_and_catalog_share_one_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[ProbeRequest] = []

    def probe(request: ProbeRequest) -> dict[str, object]:
        calls.append(request)
        return {"state": "ready", "source": "local_preflight", "models": [{"id": "model"}],
                "local": {"connected": True, "model": "model", "models": ["model"]}}

    _install_connector(monkeypatch, lambda key: _Connector(
        revision=lambda: "local-v1", probe=probe,
    ) if key == "local" else _Connector(revision=lambda: "zen-v1", probe=lambda _: {
        "state": "unavailable", "source": "api_catalog", "models": [],
    }))
    ctx = AppContext(tmp_path)
    _enable(ctx, "local", "zen")
    try:
        api.providers_response(ctx)
        assert ctx.connection_status.wait_for("local", timeout=5)
        api.local_provider_response(ctx)
        api.api_models_response(ctx)
        assert len(calls) == 1
    finally:
        ctx.close()


def test_changed_connection_discards_old_probe_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    revision = ["old"]

    def probe(request: ProbeRequest) -> dict[str, object]:
        if request.connection_revision == "old":
            entered.set()
            assert release.wait(5)
            return {"state": "ready", "source": "local_preflight", "models": [{"id": "old"}]}
        return {"state": "unavailable", "source": "local_preflight", "models": []}

    _install_connector(monkeypatch, lambda key: _Connector(
        revision=lambda: revision[0], probe=probe,
    ) if key == "local" else _Connector(revision=lambda: "zen", probe=lambda _: {
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
        local = next(row for row in _provider_rows(payload) if row["id"] == "local")
        assert local["state"] == "unavailable"
    finally:
        release.set()
        ctx.close()


def test_removed_zen_is_not_scheduled_or_imported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[str] = []

    def connection_for(provider_id: str) -> object:
        attempted.append(provider_id)
        raise AssertionError("no API connector import")

    monkeypatch.setattr("codey.providers.api_connections.connection_for", connection_for)
    # Restore insertion order too; delitem's undo appends Zen after Cline.
    with _without_zen_registration():
        ctx = AppContext(tmp_path)
        try:
            _, payload = api.providers_response(ctx)
            assert all(row["id"] != "zen" for row in _provider_rows(payload))
            assert "zen" not in attempted
        finally:
            ctx.close()


def test_failed_refresh_preserves_last_ready_result_as_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result: list[dict[str, object]] = [{"state": "ready", "source": "local_preflight", "models": [{"id": "old"}]}]
    connector = _Connector(revision=lambda: "same", probe=lambda _: result[0])
    _install_connector(monkeypatch, lambda _: connector)
    ctx = AppContext(tmp_path)
    _enable(ctx, "local")
    try:
        ctx.connection_status.refresh("local")
        assert ctx.connection_status.wait_for("local", timeout=5).state == "ready"
        result[0] = {"state": "unavailable", "source": "local_preflight", "error": "timeout",
                     "models": list[dict[str, object]]()}
        ctx.connection_status.refresh("local", force=True)
        snapshot = ctx.connection_status.wait_for("local", timeout=5)
        assert (snapshot.state, snapshot.freshness, snapshot.error) == ("ready", "stale", "timeout")
        assert ctx.connection_status.data("local")["models"] == [{"id": "old"}]
    finally:
        ctx.close()


def test_failed_stale_refresh_is_not_advisor_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.app import provider_services

    responses: list[dict[str, object]] = [
        {"state": "ready", "source": "api_catalog", "models": [{"id": "model"}]},
        {"state": "unavailable", "source": "api_catalog", "error": "timeout", "models": []},
    ]
    connector = _Connector(
        revision=lambda: "same",
        probe=lambda _request: responses.pop(0),
    )
    _install_connector(monkeypatch, lambda _: connector)
    ctx = AppContext(tmp_path)
    _enable(ctx, "zen")
    provider_services.reset_provider_availability_cache()
    try:
        ctx.connection_status.refresh("zen")
        assert ctx.connection_status.wait_for("zen", timeout=5).state == "ready"
        ctx.connection_status.refresh("zen", force=True)
        snapshot = ctx.connection_status.wait_for("zen", timeout=5)
        assert (snapshot.state, snapshot.freshness, snapshot.error) == ("ready", "stale", "timeout")
        assert provider_services.provider_availability(ctx) == {"zen": False}
    finally:
        provider_services.reset_provider_availability_cache()
        ctx.close()


def test_refresh_keeps_last_ready_status_visible_while_probe_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = [0]

    def probe(_request: ProbeRequest) -> dict[str, object]:
        calls[0] += 1
        if calls[0] == 2:
            entered.set()
            assert release.wait(5)
        return {"state": "ready", "source": "local_preflight", "models": [{"id": "model"}]}

    _install_connector(monkeypatch, lambda _: _Connector(
        revision=lambda: "same", probe=probe,
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


def test_local_probe_reports_bootstrap_result(monkeypatch: pytest.MonkeyPatch) -> None:
    local: dict[str, object] = {
        "connected": True, "model": "fast", "models": ["fast"], "base_url": "http://localhost:9999",
    }

    def bootstrap(**kwargs: object) -> dict[str, object]:
        return local

    def payload(value: dict[str, object]) -> dict[str, object]:
        return {"id": "local", "models": [{"id": "fast"}], "local": value}

    monkeypatch.setattr(local_config, "local_bootstrap_payload", bootstrap)
    monkeypatch.setattr(local_connection, "_model_payload_from_bootstrap", payload)
    result = local_connection.probe_status(None)
    assert result["state"] == "ready"
    assert result["source"] == "local_preflight"
    assert result["local"] is local


def test_zen_probe_reports_catalog_readiness_not_request_eligibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zen_connection, "model_payload", lambda: {
        "models": [{"id": "free"}], "stale": False, "error": "",
    })
    result = zen_connection.probe_status(None)
    assert (result["state"], result["source"]) == ("ready", "api_catalog")
    assert "request_eligible" not in result
