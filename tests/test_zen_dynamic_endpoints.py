"""Official directory changes affect new selections, never an admitted run."""

import pytest

from codey.providers.zen import connection
from codey.providers.zen.catalog import ZenCatalog


def listing(endpoint):
    return {"opencode": {"api": endpoint, "npm": "@ai-sdk/openai-compatible", "models": {
        "free": {"tool_call": True, "cost": {"input": 0, "output": 0},
                 "limit": {"context": 32768, "output": 4096}},
    }}}


def test_changed_path_is_discovered_without_updating_codey_and_survives_restart(tmp_path, monkeypatch):
    endpoint = ["https://opencode.ai/inference/openai/v1"]
    calls = []

    def fetch(url, _etag):
        calls.append(url)
        if url.endswith("/models"):
            assert url == endpoint[0] + "/models"
            return {"data": [{"id": "free"}]}, ""
        return listing(endpoint[0]), "changed-etag"

    catalog = ZenCatalog(tmp_path, fetch=fetch)
    monkeypatch.setattr(connection, "catalog", lambda: catalog)
    catalog.refresh(force=True)
    admitted = connection.capture_selection({"model": "free"})
    assert connection.endpoint_for_selection(admitted) == endpoint[0]
    endpoint[0] = "https://opencode.ai/inference/new/openai/v2"
    assert catalog.refresh(force=True)[0].api == endpoint[0]
    assert connection.capture_selection({"model": "free"}).connection_revision != admitted.connection_revision
    with pytest.raises(ValueError, match="changed"):
        connection.validate_selection(admitted)
    with pytest.raises(ValueError, match="changed"):
        connection.endpoint_for_selection(admitted)
    assert ZenCatalog(tmp_path, fetch=fetch).models[0].api == endpoint[0]


@pytest.mark.parametrize("destination", [
    "http://opencode.ai/inference/v1", "https://opencode.ai.evil.test/inference/v1",
    "https://user@opencode.ai/inference/v1", "https://opencode.ai:444/inference/v1",
    "https://opencode.ai/inference/v1?forward=elsewhere", "https://127.0.0.1/inference/v1",
    "https://opencode.ai/inference/%2e%2e/v1",
])
def test_directory_cannot_move_to_an_unapproved_destination(tmp_path, destination):
    def fetch(url, _etag):
        assert not url.startswith(destination), "untrusted destination must be rejected before connecting"
        return listing(destination), ""

    catalog = ZenCatalog(tmp_path, fetch=fetch)
    assert not catalog.refresh(force=True)
    assert catalog.stale
    assert catalog.error


def test_generation_endpoint_does_not_have_to_offer_a_models_route(tmp_path):
    from codey.providers.zen.catalog import DIRECTORY_URL

    calls = []
    def fetch(url, _etag):
        calls.append(url)
        if url == DIRECTORY_URL:
            return listing("https://opencode.ai/inference/openai/v1"), "etag"
        raise OSError("404: generation API has no model-list route")

    catalog = ZenCatalog(tmp_path, fetch=fetch)
    assert [model.id for model in catalog.refresh(force=True)] == ["free"]
    assert calls == [DIRECTORY_URL]
