"""KoboldCpp factories count the complete template using the admitted running model."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


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
            assert requests[0][1]["tool_choice"] == "required"
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
