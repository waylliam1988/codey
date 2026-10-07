"""Removing a connector blocks sends while preserving readable stored chats."""
import pytest

from codey.providers.catalog import API_CONNECTIONS
from codey.providers.registry import connect_provider
from codey.storage.ui_state_store import UiStateStore


def test_ui_store_keeps_generic_model_and_xhigh_effort_without_credentials(tmp_path):
    store = UiStateStore(tmp_path)
    selection = {"connection_id": "zen", "base_url": "", "model": "response-free", "effort": "xhigh", "efforts": {"response-free": "xhigh"}}
    store.save({"sessions": [{"id": "chat", "provider": "zen", "modelSelection": {**selection, "api_key": "discard"}}], "active_id": "chat", "projects": []}, base_revision=0)
    assert store.load()["sessions"][0]["modelSelection"] == selection


def test_removed_connection_cannot_open_but_old_chat_is_readable(tmp_path, monkeypatch):
    store = UiStateStore(tmp_path)
    store.save({"sessions": [{"id": "chat", "provider": "zen", "messages": []}], "active_id": "chat", "projects": []}, base_revision=0)
    monkeypatch.delitem(API_CONNECTIONS, "zen")
    with pytest.raises(ValueError, match="unsupported provider"):
        connect_provider("zen")
    assert store.load()["sessions"][0]["provider"] == "zen"
