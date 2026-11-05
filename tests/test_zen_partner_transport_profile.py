"""Partner environment context is truthful and a missing runtime is not a quota."""
import json
import sys

import pytest

from codey.providers import api_generation
from codey.providers.api_transport import GenerationNotSentError
from codey.providers.base import AssistantTurn
from codey.providers.zen import connection, node_http
from codey.providers.zen.catalog import ZenCatalog


def test_production_selection_sends_environment_without_copying_another_harness(tmp_path, monkeypatch):
    directory = {"opencode": {"api": "https://opencode.ai/inference/openai/v1",
        "npm": "@ai-sdk/openai-compatible", "models": {
            "fixture": {"tool_call": True, "cost": {"input": 0, "output": 0},
                        "limit": {"context": 32768, "output": 4096}},
        }}}
    catalog = ZenCatalog(tmp_path, fetch=lambda *_: (directory, "etag"))
    monkeypatch.setattr(connection, "catalog", lambda: catalog)
    requests = []

    def generate(endpoint, payload, headers, **kwargs):
        requests.append(payload)
        return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_generation, "generate", generate)
    provider = connection.open_selection(connection.capture_selection({"model": "fixture"}))
    try:
        assert provider.send("Reply OK", timeout=2) == "OK"
        assert len(requests) == 1
        system = requests[0]["messages"][0]["content"]
        assert "<env>" in system and "</env>" in system
        assert f"Platform: {sys.platform}" in system
        assert "authorized workspace" in system and "no filesystem access" in system
        assert str(tmp_path) not in system
        assert "running in OpenCode" not in system
        assert "tools.opencode" not in json.dumps(requests[0])
    finally:
        provider.close()


def test_missing_bun_fails_before_submission_instead_of_falling_back_to_node(tmp_path, monkeypatch):
    monkeypatch.setattr(node_http.shutil, "which", lambda name: "/unused/node" if name == "node" else None)
    monkeypatch.setattr(node_http.Path, "home", lambda: tmp_path)
    with pytest.raises(GenerationNotSentError, match="requires Bun") as error:
        node_http.fetch_runtime()
    assert error.value.reason_code == "not_submitted"


@pytest.mark.parametrize("method", ["send_turn", "send_tool_results", "send_tool_results_bound"])
def test_wrapper_forwards_kernel_options_to_an_installed_runtime_without_required_call_support(method):
    calls = []

    class InstalledRuntime:
        def send_turn(self, data, tools, timeout=None, *, tool_call_observations="",
                      tool_call_history_groups=0, output_token_limit=0):
            calls.append((tool_call_observations, tool_call_history_groups, output_token_limit))
            return AssistantTurn(text="OK")

        send_tool_results = send_turn

        def send_tool_results_bound(self, data, window_ref, tools, timeout=None, delivery_commit=None,
                                    *, tool_call_observations="", tool_call_history_groups=0, output_token_limit=0):
            return self.send_turn(data, tools, timeout, tool_call_observations=tool_call_observations,
                                  tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit)

    provider = connection.ZenProvider(InstalledRuntime())
    options = {"tool_call_observations": "read completed", "tool_call_history_groups": 2, "output_token_limit": 123}
    if method == "send_tool_results_bound":
        turn = provider.send_tool_results_bound([], object(), timeout=2, **options)
    else:
        turn = getattr(provider, method)("hello" if method == "send_turn" else [], timeout=2, **options)
    assert turn.text == "OK" and calls == [("read completed", 2, 123)]
