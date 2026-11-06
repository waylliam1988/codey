"""Registered API connections never become implicit browser failover candidates."""
from types import SimpleNamespace

from codey.app.provider_registry import ProviderRegistry
from codey.app.provider_services import reviewer_candidates
from codey.providers import api_connections


def test_api_connections_are_excluded_from_browser_failover_and_automatic_review():
    registry = ProviderRegistry()
    assert "zen" not in registry.failover_order(lambda: {"zen": True})
    state = SimpleNamespace(providers=registry)
    supervisor = SimpleNamespace(is_available=lambda _: True)
    assert "zen" not in reviewer_candidates(state, "qwen", supervisor=supervisor)


def test_cross_connection_reviewer_skips_failed_source_and_tries_next_enabled_api(monkeypatch):
    zen = SimpleNamespace(model_payload=lambda: (_ for _ in ()).throw(RuntimeError("Zen is offline")))
    cline = SimpleNamespace(
        model_payload=lambda: {"models": [{"id": "cline-model", "review_eligible": True}]},
        capture_selection=lambda selection: "cline-selection",
    )

    def connection_for(provider_id):
        if provider_id == "zen":
            return zen
        if provider_id == "cline":
            return cline
        raise AssertionError(provider_id)

    monkeypatch.setattr(api_connections, "connection_for", connection_for)

    assert api_connections.capture_cross_connection_reviewer(
        allowed_models_by_connection={"zen": {"zen-model"}, "cline": {"cline-model"}},
    ) == ("cline", "cline-selection")
