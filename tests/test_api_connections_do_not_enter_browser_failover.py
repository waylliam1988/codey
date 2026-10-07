"""Registered API connections never become implicit browser failover candidates."""
from types import SimpleNamespace

from codey.app.provider_registry import ProviderRegistry
from codey.app.provider_services import reviewer_candidates


def test_api_connections_are_excluded_from_browser_failover_and_automatic_review():
    registry = ProviderRegistry()
    assert "zen" not in registry.failover_order(lambda: {"zen": True})
    state = SimpleNamespace(providers=registry)
    supervisor = SimpleNamespace(is_available=lambda _: True)
    assert "zen" not in reviewer_candidates(state, "qwen", supervisor=supervisor)
