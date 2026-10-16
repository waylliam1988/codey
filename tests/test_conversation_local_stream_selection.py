from codey.providers import local_connection
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig


def test_native_local_selection_requests_real_streaming(monkeypatch):
    config = LocalProviderConfig(base_url="http://127.0.0.1:1234/v1", model="fixture", native_tools_mode="on",
        context=LocalContextBudget(32768, 8192, 12000), connection_revision="test")
    monkeypatch.setattr("codey.providers.local_selection.capture_local_run_config", lambda selection: config)
    monkeypatch.setattr(local_connection._local_config, "load_local_config", lambda: config)
    monkeypatch.setattr("codey.providers.local_tokens.admitted_counter", lambda *args: ("estimated", 32768))
    selection = local_connection.capture_selection({"model": "fixture"})
    assert selection.stream is True
