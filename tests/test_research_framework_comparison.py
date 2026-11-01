"""The comparison changes sampling/budgets, never model messages or tools."""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.manual.research_framework_comparison import InferenceProxy, sampling


@contextmanager
def upstream(*, stream=False, fail=False):
    seen = []
    content = (b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n'
               if stream else b'{"choices":[{"message":{"content":"answer"}}]}')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = json.dumps({"last_seed": 74, "last_input_count": 20, "last_token_count": 5}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(429 if fail else 200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen, content
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def send(base, payload):
    return urlopen(Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(),
                           headers={"Content-Type": "application/json"}), timeout=5)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_real_proxy_preserves_messages_native_tools_schema_and_response_bytes(tmp_path, stream, native):
    payload = {"model": "fixture", "messages": [{"role": "user", "content": "complete OR conditions"}],
               "stream": stream, "max_tokens": 6000}
    if native:
        payload["tools"] = [{"type": "function", "function": {"name": "read", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
    else:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "choice", "schema": {
            "type": "object", "properties": {"state": {"enum": ["unresolved"]}}}}}
    wire = tmp_path / "wire.jsonl"
    with upstream(stream=stream) as (url, seen, content):
        proxy = InferenceProxy(wire, 74, upstream=url)
        with proxy as base, send(base, payload) as response:
            assert response.read() == content
        assert seen == [sampling(payload, 74)]
        assert seen[0]["messages"] == payload["messages"]
        assert (seen[0]["tools"] == payload["tools"] if native else
                seen[0]["response_format"] == payload["response_format"])
        assert seen[0]["max_tokens"] == 4096 and seen[0]["seed"] == 74
        assert payload["max_tokens"] == 6000
        assert proxy.requests == 1 and proxy.tokens == 20
    rows = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["original_payload"] == payload
    assert rows[-1]["response_text"] == content.decode()


def test_budget_rejection_never_reaches_the_model(tmp_path):
    with upstream() as (url, seen, _), InferenceProxy(tmp_path / "wire.jsonl", 74, upstream=url, request_limit=1) as base:
        payload = {"messages": [{"role": "user", "content": "question"}]}
        with send(base, payload) as response:
            response.read()
        with pytest.raises(HTTPError) as error:
            send(base, payload)
        assert error.value.code == 400
        assert json.loads(error.value.read())["error"]["code"] == "evaluation_budget"
        assert len(seen) == 1


def test_provider_failure_is_preserved_instead_of_becoming_a_successful_answer(tmp_path):
    with upstream(fail=True) as (url, seen, content), InferenceProxy(tmp_path / "wire.jsonl", 74, upstream=url) as base:
        with pytest.raises(HTTPError) as error:
            send(base, {"messages": []})
        assert error.value.code == 429 and error.value.read() == content
        assert len(seen) == 1


def test_report_extraction_excludes_pretool_progress_and_earlier_assistant_messages():
    from tests.manual.research_framework_comparison import final_report

    events = [{"type": "text", "part": {"messageID": "a", "text": "preliminary guess"}},
              {"type": "tool_use", "part": {"tool": "read"}},
              {"type": "text", "part": {"messageID": "b", "text": "superseded"}},
              {"type": "text", "part": {"messageID": "c", "text": "final finding"}},
              {"type": "text", "part": {"messageID": "c", "text": "specific gap"}}, {"type": "done"}]
    assert final_report(events) == "final finding\n\nspecific gap"
    assert final_report(events[:2]) == ""


def test_read_channel_separates_the_task_from_identical_lossless_evidence(tmp_path):
    from tests.manual.research_framework_comparison import material_text, task_text, write_json

    doc = {"title": "Official source", "url": "https://example.org/source",
           "text": "complete source: A OR B; except C; not D"}
    write_json(tmp_path / "wal-material.json", [doc])
    inline = task_text(tmp_path, "wal_synthesis")
    evidence = material_text(tmp_path, "wal_synthesis")
    assert doc["text"] in inline
    write_json(tmp_path / "material-channel.json", {"mode": "read"})
    reading = task_text(tmp_path, "wal_synthesis")
    assert doc["text"] not in reading and doc["url"] in reading
    assert material_text(tmp_path, "wal_synthesis") == evidence


def test_configured_budget_survives_the_real_save_reload_and_server_admission(tmp_path, monkeypatch):
    from codey.providers import local_config, local_tokens
    from tests.manual.research_framework_comparison import comparison_config

    monkeypatch.setattr(local_config, "DEFAULT_STATE_HOME", tmp_path)
    config = local_config.LocalProviderConfig(base_url="http://localhost:5001/v1", model="fixture")
    local_config.save_local_config(comparison_config(config))
    loaded = local_config.load_local_config()

    def metadata(url, _key):
        return ({"result": "KoboldCpp", "jinja": True} if url.endswith("/version") else {"value": 262144})

    monkeypatch.setattr(local_tokens, "_metadata_json", metadata)
    counter, budget = local_tokens.admitted_context_budget(loaded.base_url, "", loaded.api_protocol, loaded.context)
    assert counter == "koboldcpp"
    assert (budget.context_window_tokens, budget.context_reserve_tokens) == (32768, 4096)
    assert config.context is None


def test_busy_backend_stops_the_next_trial_before_inference():
    from tests.manual.research_framework_comparison import backend_ready

    with upstream() as (url, seen, _), pytest.raises(RuntimeError, match="backend not idle"):
        backend_ready(url)
    assert seen == []


def test_generation_owner_key_is_forwarded_without_becoming_a_model_message(tmp_path):
    payload = {"messages": [{"role": "user", "content": "question"}]}
    with upstream() as (url, seen, _), InferenceProxy(
            tmp_path / "wire.jsonl", 74, upstream=url, genkey="evaluation-owned") as base:
        with send(base, payload) as response:
            response.read()
        assert seen[0]["genkey"] == "evaluation-owned"
        assert seen[0]["messages"] == payload["messages"]


@pytest.mark.parametrize("invalid", [{"turns": True}, {"requests": 0}, {"unknown": 24}, [24]])
def test_exploratory_budget_cannot_silently_use_invalid_or_unknown_limits(tmp_path, invalid):
    from tests.manual.research_framework_comparison import experiment_limits, write_json

    write_json(tmp_path / "limits.json", invalid)
    with pytest.raises(ValueError, match="positive integer"):
        experiment_limits(tmp_path)


def test_budget_extension_reaches_the_real_agent_configuration_and_preserves_main_defaults(tmp_path):
    from tests.manual.research_framework_comparison import experiment_limits, openscience_environment, write_json

    assert experiment_limits(tmp_path) == {"turns": 12, "requests": 12, "process_seconds": 360, "agent_seconds": 300}
    write_json(tmp_path / "limits.json", {"turns": 24, "requests": 24, "process_seconds": 660, "agent_seconds": 600})
    openscience_environment(tmp_path, tmp_path, "http://localhost:5001", "fixture")
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["agent"]["research"]["steps"] == 24
    assert experiment_limits(tmp_path)["requests"] == 24
