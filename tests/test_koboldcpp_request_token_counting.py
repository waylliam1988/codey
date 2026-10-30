"""KoboldCpp factories count the complete template using the admitted running model."""
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("entrypoint", ["admitted", "direct"])
@pytest.mark.parametrize("mode,expected", [("auto", 262144), ("manual", 131072), ("env", 65536)])
def test_production_local_budget_follows_server_unless_explicitly_capped(tmp_path, monkeypatch, entrypoint, mode, expected):
    from codey.providers import local_config, local_connection, local_tokens

    for key in ("LOCAL_OPENAI_CONTEXT_WINDOW", "LOCAL_OPENAI_CONTEXT_RESERVE", "LOCAL_OPENAI_CONTEXT_KEEP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(local_config, "DEFAULT_STATE_HOME", tmp_path)
    config = local_config.LocalProviderConfig(base_url="http://localhost:9/v1", model="fixture", connection_revision="test")
    if mode == "manual":
        config = replace(config, context=local_config.context_budget_for_window(expected))
    if mode == "env":
        monkeypatch.setenv("LOCAL_OPENAI_CONTEXT_WINDOW", str(expected))
    local_config.save_local_config(config)
    maximum = [262144]
    monkeypatch.setattr(local_tokens, "_metadata_json", lambda url, key: (
        {"result": "KoboldCpp", "jinja": True} if url.endswith("/version") else {"value": maximum[0]}))
    monkeypatch.setattr(local_connection._local_discovery, "resolve_local_endpoint", lambda **kwargs: SimpleNamespace(
        base_url=config.base_url, models=(config.model,)))
    if entrypoint == "admitted":
        selection = local_connection.capture_selection({"base_url": config.base_url, "model": config.model})
        assert selection.context_window_tokens == expected
        assert selection.token_counter == "koboldcpp"
        assert selection.budget_source == ("server" if mode == "auto" else "server_and_configuration")
        # A resumed task keeps the admitted budget, even after settings/server growth.
        maximum[0] = 524288
        local_config.save_local_config(replace(config, context=local_config.context_budget_for_window(32768)))
        monkeypatch.setenv("LOCAL_OPENAI_CONTEXT_WINDOW", "32768")
        provider = local_connection.open_selection(selection)
        assert provider.context_budget.window_tokens == expected
        assert provider.context_budget.output_tokens + provider.context_budget.safety_tokens == selection.context_reserve_tokens
        if mode == "auto":
            monkeypatch.delenv("LOCAL_OPENAI_CONTEXT_WINDOW")
            local_config.save_local_config(config)
            assert local_connection.capture_selection({"base_url": config.base_url, "model": config.model}).context_window_tokens == maximum[0]
    else:
        provider = local_connection.connect_local(config=config)
    assert provider.context_budget.window_tokens == expected
    if mode == "auto":
        assert provider.context_budget.output_tokens + provider.context_budget.safety_tokens == 32768
        assert provider.context_budget.keep_recent_tokens == 32000


def test_automatic_budget_does_not_invent_capacity_for_unconfirmed_server(monkeypatch):
    from codey.providers import local_config, local_tokens

    monkeypatch.setattr(local_tokens, "_metadata_json", lambda url, key: {})
    counter, budget = local_tokens.admitted_context_budget("http://localhost:9/v1", "", "openai-completions",
        local_config.LocalContextBudget(32768, 8192, 12000))
    assert counter == "estimated" and budget.context_window_tokens == 32768


@pytest.mark.parametrize("entrypoint", ["admitted", "direct"])
@pytest.mark.parametrize("request_mode", ["text", "tools"])
def test_local_factory_freezes_kobold_capability_clamps_window_and_counts_before_generating(tmp_path, monkeypatch, entrypoint, request_mode):
    from codey.providers import local_config, local_connection
    from codey.providers.base import ProviderToolDefinition

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            bodies = {"/api/extra/version": {"result": "KoboldCpp", "jinja": True},
                      "/api/extra/true_max_context_length": {"value": 4096},
                      "/api/v1/model": {"result": "fixture"},
                      "/v1/models": {"data": [{"id": "fixture"}]}}
            self.respond(bodies.get(self.path, {}))

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload))
            self.respond({"value": 88} if self.path == "/api/extra/tokencount" else
                         {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setattr(local_config, "DEFAULT_STATE_HOME", tmp_path)
    local_config.save_local_config(local_config.LocalProviderConfig(
        base_url=base, model="fixture", thinking_enabled=False,
        context=local_config.LocalContextBudget(8192, 1024, 2048)))
    try:
        if entrypoint == "admitted":
            selection = local_connection.capture_selection({"base_url": base, "model": "fixture", "thinking": False})
            assert selection.context_window_tokens == 4096
            provider = local_connection.open_selection(selection)
        else:
            provider = local_connection.connect_local(config=local_config.load_local_config())
        assert provider.context_budget.window_tokens == 4096
        if request_mode == "tools":
            turn = provider.send_turn("hello", [ProviderToolDefinition("read", "Read file", {"type": "object"})])
            assert turn.text == "ok"
        else:
            assert provider.send("hello") == "ok"
        assert [path for path, _ in requests] == ["/api/extra/tokencount", "/v1/chat/completions"]
        assert requests[0][1] == requests[1][1]
        assert requests[0][1]["chat_template_kwargs"] == {"enable_thinking": False}
        if request_mode == "tools":
            assert requests[0][1]["tools"][0]["function"]["name"] == "read"
            assert requests[0][1]["tool_choice"] == "auto"
        assert provider.last_context_count.method == "tokenizer"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


@pytest.mark.parametrize("changed", ["model", "window", "count"])
def test_changed_model_reduced_running_window_and_invalid_count_are_rejected(monkeypatch, changed):
    import time

    from codey.providers.error_classification import RequestPrepError
    from codey.providers.local_tokens import KoboldRequestCounter

    counter = KoboldRequestCounter("http://localhost:9/v1", "fixture", "", 4096)
    called = []

    def respond(path, deadline, payload=None):
        called.append(path)
        if path == "/api/v1/model":
            return {"result": "other" if changed == "model" else "fixture"}
        if path == "/api/extra/true_max_context_length":
            return {"value": 2048 if changed == "window" else 4096}
        return {"value": True}

    monkeypatch.setattr(counter, "_json", respond)
    with pytest.raises(RequestPrepError):
        counter({"messages": [{"role": "user", "content": "hello"}]}, deadline=time.monotonic() + 5)
    if changed != "count":
        assert "/api/extra/tokencount" not in called


@pytest.mark.parametrize("path", ["/api/v1/model", "/api/extra/tokencount"])
def test_malformed_metadata_gets_one_fresh_read_without_generation(monkeypatch, path):
    import time
    from unittest.mock import MagicMock

    from codey.providers.local_tokens import KoboldRequestCounter

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = [b'HTTP/1.0 200 OK\r\ncontent-type: application/json\r\n\r\n{"value":88}',
                                 b'{"value":88}']
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    counter = KoboldRequestCounter("http://localhost:9/v1", "fixture", "", 4096)
    assert counter._json(path, time.monotonic() + 5, {"messages": []} if "tokencount" in path else None) == {"value": 88}
    assert opener.open.call_count == 2
    assert all(call.args[0].full_url.endswith(path) for call in opener.open.call_args_list)
    # A second malformed response fails closed; never estimate or strip headers.
    response.read.side_effect = [b'not json', b'not json']
    from codey.providers.error_classification import RequestPrepError
    with pytest.raises(RequestPrepError, match="invalid JSON"):
        counter._json(path, time.monotonic() + 5)
