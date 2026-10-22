"""Zen catalog contracts, cached outages, free eligibility, and header scope."""
import json
import re
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from codey.providers.zen.catalog import ZenCatalog, fetch_catalog, parse_models
from codey.providers.zen.identity import ZenIdentity


def directory():
    return {"opencode": {"npm": "@ai-sdk/openai-compatible", "api": "https://opencode.ai/inference/openai/v1", "models": {
        "chat-free": {"name": "Chat Free", "tool_call": True, "cost": {"input": 0, "output": 0}, "limit": {"context": 32768, "output": 4096}},
        "response-free": {"limit": {"context": 131072, "output": 8192}, "name": "Responses Free", "tool_call": True, "cost": {"input": 0, "output": 0}, "provider": {"npm": "@ai-sdk/openai"}},
        "paid": {"tool_call": True, "cost": {"input": 1, "output": 0}},
        "unknown-price": {"tool_call": True, "cost": {"output": 0}},
        "other-protocol": {"tool_call": True, "cost": {"input": 0, "output": 0}, "provider": {"npm": "@ai-sdk/anthropic"}},
    }}}


def test_catalog_uses_model_protocol_override_and_explicit_zero_cost():
    models = parse_models(directory())
    assert {m.id: m.protocol for m in models} == {"chat-free": "openai-completions", "response-free": "openai-responses"}


def test_catalog_preserves_boolean_reasoning_toggle_capability():
    listing = directory()
    listing["opencode"]["models"]["chat-free"]["reasoning"] = True
    listing["opencode"]["models"]["chat-free"]["reasoning_options"] = [{"type": "toggle"}]

    model = next(model for model in parse_models(listing) if model.id == "chat-free")

    assert model.thinking_toggle is True
    assert model.efforts == ()
    assert model.to_payload()["thinking_toggle"] is True


def test_catalog_preserves_toggle_and_effort_capabilities_together_and_deduplicates_efforts():
    listing = directory()
    listing["opencode"]["models"]["chat-free"]["reasoning_options"] = [
        {"type": "toggle"},
        {"type": "effort", "values": ["low", "high", "low", "budget_tokens", "max"]},
    ]

    model = next(model for model in parse_models(listing) if model.id == "chat-free")

    assert model.thinking_toggle is True
    assert model.efforts == ("low", "high", "max")


def test_catalog_accepts_current_inference_api_destination():
    listing = directory()
    listing["opencode"]["api"] = "https://opencode.ai/inference/openai/v1"

    assert parse_models(listing)


@pytest.mark.parametrize("status", [304, 403])
def test_catalog_closes_http_error_stream_and_preserves_response_classification(monkeypatch, status):
    from codey.providers.zen import catalog

    stream = BytesIO(b"catalog error")
    error = HTTPError("https://models.opencode.ai/api.json", status, "fixture", {}, stream)

    def reject(request, *, timeout):
        raise error

    monkeypatch.setattr(catalog.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=reject))
    if status == 304:
        assert fetch_catalog(error.url, "fixture-etag") == (None, "fixture-etag")
    else:
        with pytest.raises(HTTPError) as caught:
            fetch_catalog(error.url, "fixture-etag")
        assert caught.value is error
        assert caught.value.code == status
    assert stream.closed


@pytest.mark.parametrize("field,value", [("provider", ["invalid"]), ("limit", ["invalid"]), ("reasoning_options", ["invalid"])])
def test_malformed_model_metadata_does_not_hide_other_free_models(field, value):
    listing = directory()
    listing["opencode"]["models"]["chat-free"][field] = value
    parsed = parse_models(listing)
    assert "response-free" in {model.id for model in parsed}


def test_catalog_keeps_cache_on_network_failure_then_removes_delisted_model(tmp_path):
    snapshots = [directory(), OSError("offline"), {"opencode": {"npm": "@ai-sdk/openai-compatible", "models": {}}}]

    def fetch(url, etag):
        if url.endswith('/models'):
            return {"data": [{"id": "chat-free"}, {"id": "response-free"}]}, ""
        value = snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return value, "etag-fixture"

    catalog = ZenCatalog(tmp_path, fetch=fetch)
    assert len(catalog.refresh(force=True)) == 2
    assert len(catalog.refresh(force=True)) == 2
    assert catalog.stale is True
    assert len(ZenCatalog(tmp_path, fetch=fetch).models) == 2
    assert catalog.refresh(force=True) == ()


def test_zen_identity_rejects_other_destinations_and_uses_upstream_session_shape():
    identity = ZenIdentity()
    first = identity.headers("https://opencode.ai/inference/openai/v1")
    second = identity.headers("https://opencode.ai/inference/openai/v1")
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", first["x-opencode-session-id"])
    assert first["x-opencode-session-id"] == second["x-opencode-session-id"]
    assert first["x-opencode-request"] != second["x-opencode-request"]
    assert "OpenCode" not in json.dumps({"prompt": "You are a coding assistant."})
    for url in ["http://127.0.0.1:1234/v1", "https://other.test/v1", "https://opencode.ai/other", "https://opencode.ai.evil.test/zen/v1"]:
        with pytest.raises(ValueError, match="destination"):
            identity.headers(url)


def test_upstream_identifier_resets_counter_when_timestamp_changes(monkeypatch):
    from codey.providers.zen import identity

    stamps = iter([1000.0, 1000.0, 1000.001])
    monkeypatch.setattr(identity.time, "time", lambda: next(stamps))
    monkeypatch.setattr(identity, "_COUNTER", 0)
    values = [identity.identifier("ses") for _ in range(3)]
    expected = [(1_000_000 * 4096 + 1), (1_000_000 * 4096 + 2), (1_000_001 * 4096 + 1)]
    assert [int(value[4:16], 16) for value in values] == expected


@pytest.mark.parametrize("limits", [{}, {"context": 262144}, {"output": 8192}])
def test_zen_models_without_explicit_context_and_output_limits_are_not_admitted(limits):
    directory = {"opencode": {"api": "https://opencode.ai/inference/openai/v1", "npm": "@ai-sdk/openai-compatible", "models": {
        "incomplete": {"tool_call": True, "cost": {"input": 0, "output": 0}, "limit": limits}}}}
    assert parse_models(directory) == ()


def test_selected_model_without_capacity_explains_why_it_cannot_be_admitted(tmp_path):
    listing = {"opencode": {"api": "https://opencode.ai/inference/openai/v1", "npm": "@ai-sdk/openai-compatible", "models": {
        "incomplete": {"tool_call": True, "cost": {"input": 0, "output": 0}}}}}
    catalog = ZenCatalog(tmp_path, fetch=lambda url, etag: (
        {"data": [{"id": "incomplete"}]} if url.endswith("/models") else listing, ""))
    with pytest.raises(ValueError, match="context and output limits"):
        catalog.require("incomplete")


def test_model_output_reservation_fits_small_selected_context(monkeypatch):
    from codey.providers.zen import connection
    from codey.providers.zen.catalog import ZenModel

    monkeypatch.setattr(connection, "catalog", lambda: SimpleNamespace(
        require=lambda _: ZenModel("small", "Small", "openai-completions", 4096, 8192, "https://opencode.ai/inference/openai/v1")))
    selection = connection.capture_selection({"model": "small"})
    assert selection.output_tokens == 1024


def test_zen_toggle_selection_becomes_explicit_thinking_flag(monkeypatch):
    from types import SimpleNamespace

    from codey.providers.zen import connection
    from codey.providers.zen.catalog import ZenModel

    monkeypatch.setattr(connection, "catalog", lambda: SimpleNamespace(
        require=lambda _: ZenModel("toggle", "Toggle", "openai-completions", 32768, 8192, "https://opencode.ai/inference/openai/v1",
                                   thinking_toggle=True)))

    enabled = connection.capture_selection({"model": "toggle", "thinking": True})
    disabled = connection.capture_selection({"model": "toggle", "thinking": False})

    assert enabled.thinking_enabled is True and enabled.reasoning_effort is None
    assert disabled.thinking_enabled is False and disabled.reasoning_effort is None


def test_zen_toggle_and_effort_model_compiles_effort_axis_without_thinking_flag(monkeypatch):
    from codey.providers.zen import connection
    from codey.providers.zen.catalog import ZenModel

    monkeypatch.setattr(connection, "catalog", lambda: SimpleNamespace(
        require=lambda _: ZenModel("combined", "Combined", "openai-completions", 32768, 8192,
                                   "https://opencode.ai/inference/openai/v1",
                                   efforts=("low", "high", "max"), thinking_toggle=True)))

    default = connection.capture_selection({"model": "combined"})
    high = connection.capture_selection({"model": "combined", "effort": "high"})

    assert default.reasoning_effort is None and default.thinking_enabled is None
    assert high.reasoning_effort == "high" and high.thinking_enabled is None
    with pytest.raises(ValueError, match="standalone thinking toggle"):
        connection.capture_selection({"model": "combined", "thinking": True})
