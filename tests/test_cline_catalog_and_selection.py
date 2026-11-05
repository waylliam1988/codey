"""Only exact fresh free IDs and known capability contracts are admitted."""
import json
from dataclasses import replace

import pytest

from codey.providers.cline import connection
from codey.providers.cline.http import JsonReply
from tests.support.cline import MODEL, replace_account, resource


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    instance = resource(tmp_path)
    monkeypatch.setattr(connection, "runtime", lambda: instance)
    yield instance
    instance.close()


def test_nonempty_public_catalog_does_not_authenticate_account(tmp_path):
    instance = resource(tmp_path, signed_in=False)
    payload = instance.payload()
    assert payload["models"] and payload["state"] == "unavailable"
    assert payload["authentication"]["state"] == "signed_out"


def test_free_eligibility_ignores_price_names_and_nonfree_groups(runtime):
    with pytest.raises(ValueError, match="free directory"):
        connection.capture_selection({"model": "paid"})
    runtime.catalog.data["free"].append({"id": "called-free", "name": "Free"})
    assert runtime.catalog.capability("called-free") is None
    with pytest.raises(ValueError, match="capability contract"):
        connection.capture_selection({"model": "called-free"})


def test_free_removal_rejects_new_requests_without_replacing_model(runtime):
    selection = connection.capture_selection({"model": MODEL})
    runtime.catalog.http.free = []
    runtime.catalog.refresh(force=True)
    with pytest.raises(ValueError, match="free directory"):
        connection.validate_selection(selection)
    assert selection.model_id == MODEL


def test_refresh_failure_preserves_list_but_stale_eligibility_blocks(runtime):
    before = runtime.catalog.payload()["models"]
    runtime.catalog.data["checked_at"] = 1
    runtime.catalog.http.hook = lambda *_: JsonReply(503, {})
    runtime.catalog.refresh(force=True)
    assert runtime.catalog.payload()["models"] == before
    assert runtime.catalog.payload()["stale"] is True
    with pytest.raises(ValueError, match="could not be refreshed"):
        connection.capture_selection({"model": MODEL})


def test_selection_revision_tracks_account_and_model_contract_only(runtime):
    selection = connection.capture_selection({"model": MODEL})
    runtime.auth.store.save(replace(runtime.auth.store.load(), token_version=42, access_token="ROTATED"))
    connection.validate_selection(selection)
    runtime.catalog.data["free"].append({"id": "unrelated", "name": "New"})
    assert connection.capture_selection({"model": MODEL}).connection_revision == selection.connection_revision
    replace_account(runtime, "account-B")
    with pytest.raises(ValueError, match="account or model contract changed"):
        connection.validate_selection(selection)
    assert "ACCESS_MARKER" not in json.dumps(selection.to_payload())


@pytest.mark.parametrize("bad", [{"model": MODEL, "effort": "max"}, {"model": MODEL, "effort": "high", "thinking": True},
    {"model": MODEL, "thinking": "on"}, {"model": MODEL, "base_url": "https://evil.example"},
    {"model": MODEL, "Authorization": "secret"}, None, {"model": ""}])
def test_invalid_or_endpoint_overriding_selection_fails(runtime, bad):
    with pytest.raises(ValueError):
        connection.capture_selection(bad)


def test_unknown_limits_and_reasoning_efforts_are_not_invented(runtime):
    raw = runtime.catalog.data["capabilities"]["upstage/solar-mini4"]
    raw["reasoning"].pop("supported_efforts")
    capability = runtime.catalog.capability(MODEL)
    assert capability.efforts == () and capability.toggle is True
    raw["top_provider"].pop("max_completion_tokens")
    payload = runtime.catalog.payload()
    assert not payload["models"] and payload["unavailable_models"][0]["availability"] == "capability_unknown"


def test_cache_roundtrip_and_no_slug_matching(runtime, tmp_path):
    from codey.providers.cline.catalog import Catalog

    loaded = Catalog(tmp_path, runtime.catalog.http)
    assert loaded.capability(MODEL) == runtime.catalog.capability(MODEL)
    assert loaded.capability("untrusted/solar-mini4") is None
