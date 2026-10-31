"""Review working windows are bounded projections, preserving original history."""

import copy
import json

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolResult
from codey.providers.token_accounting import RequestContextCount


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_bounded_payload_preserves_protocol_pairs_counts_actual_view_and_keeps_history(monkeypatch, native, protocol):
    counted, sent = [], []

    def count(payload, *, deadline):
        counted.append(copy.deepcopy(payload))
        return RequestContextCount(100, "tokenizer")

    def generate(_url, payload, *_args, **_kwargs):
        sent.append(copy.deepcopy(payload))
        if protocol == "openai-completions":
            return {"choices": [{"message": {"content": "recorded"}, "finish_reason": "stop"}]}
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "recorded"}]}], "status": "completed"}

    monkeypatch.setattr(api_transport, "generate", generate)
    history = []
    for n in range(8):
        history.extend([{"role": "user", "content": f"ARCHIVED_SCOPE_{n}" + "x" * 2000},
                        {"role": "assistant", "content": f"old judgment {n}"}])
    if native:
        if protocol == "openai-responses":
            history.append({"type": "reasoning", "id": "r-latest", "encrypted_content": "preserve-reasoning"})
        history.append({"role": "assistant", "content": "latest submission", "tool_calls": [
            {"id": "current-call", "type": "function",
             "function": {"name": "submit_research_review", "arguments": '{"assessment":{}}'}}]})
        history.append({"role": "tool", "tool_call_id": "current-call", "content": "batch recorded"})
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, api_protocol=protocol,
                           request_counter=count, stream=False)
    try:
        provider.load_durable_context(history, source_entry_ids=tuple(f"entry-{n}" for n in range(len(history))))
        before = copy.deepcopy(provider._messages)
        if native:
            provider.send_tool_results([ProviderToolResult("current-call", "batch recorded", "Continue current batch.")],
                tool_call_observations="FROZEN CURRENT SCOPE", tool_call_history_groups=2)
        else:
            provider.send("Continue current batch.", tool_call_observations="FROZEN CURRENT SCOPE", tool_call_history_groups=2)
        assert counted == sent
        assert len(sent) == 1
        serialized = json.dumps(sent[0])
        assert "ARCHIVED_SCOPE_" not in serialized
        assert serialized.count("FROZEN CURRENT SCOPE") == 1
        assert provider._messages[:len(before)] == before
        assert "ARCHIVED_SCOPE_0" in json.dumps(provider._messages)
        if native:
            assert serialized.count("current-call") == 2
            assert "batch recorded" in serialized
            if protocol == "openai-responses":
                assert "preserve-reasoning" in serialized
        # Ordinary sends still use the complete history; this is a frozen
        # request option, never a sticky provider mode or new context writer.
        provider.send("Ordinary continuation")
        assert "ARCHIVED_SCOPE_0" in json.dumps(sent[-1])
    finally:
        provider.close()


@pytest.mark.parametrize("groups", [True, -1, 17, "2"])
def test_invalid_bounded_history_is_rejected_before_any_send(groups):
    provider = ApiProvider("http://fixture/v1", "fixture")
    try:
        with pytest.raises(ValueError, match="bounded request history"):
            provider.send("next", tool_call_observations="current", tool_call_history_groups=groups)
        assert not provider._messages
    finally:
        provider.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_production_review_freezes_bounded_requests_and_completes_all_batches(tmp_path, monkeypatch, native):
    from tests.test_research_semantic_review import _run

    payloads, providers = [], []

    def factory(scripted):
        def generate(_url, payload, *_args, **_kwargs):
            payloads.append(copy.deepcopy(payload))
            prompt = "\n".join(str(row.get("content", "")) for row in payload["messages"])
            raw = scripted.send(prompt)
            call = json.loads(raw)
            message = ({"tool_calls": [{"id": f"call-{len(payloads)}", "type": "function",
                "function": {"name": call["tool"], "arguments": json.dumps(call["args"])}}]}
                if native else {"content": raw})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        monkeypatch.setattr(api_transport, "generate", generate)
        provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False)
        providers.append(provider)
        return provider

    try:
        task = "Explain writer concurrency. " * 12 + "Distinguish historical version restrictions."
        result = _run(tmp_path, task=task, budget=60, provider_factory=factory)
        assert result.stop_reason == "done", result.summary
        review = [row for row in payloads if "FROZEN BASIS:\\n" in json.dumps(row)]
        assert len(review) > 4
        for payload in review:
            assert json.dumps(payload).count("FROZEN BASIS:") == 1
            assert len(payload["messages"]) <= 6
        frozen = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "managed_outputs").rglob("*.json")
                  if not path.name.endswith(".meta.json")]
        assert any('"tool_call_history_groups": 2' in json.dumps(row) for row in frozen)
    finally:
        for provider in providers:
            provider.close()
