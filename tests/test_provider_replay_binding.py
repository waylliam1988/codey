from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codey.providers.api_codec import GenerationSettings
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.providers.replay import transform_replay_messages
from codey.types.json_value import JsonObject
from codey.types.protocol_window import ANTHROPIC_BLOCKS, REPLAY_BINDING, RESPONSES_REASONING, ProviderBinding


def test_replay_reasoning_requires_original_connection_protocol_model_and_call_id() -> None:
    binding = ProviderBinding("primary", "r1", "model", "openai-completions", "contract")
    row: JsonObject = {"role": "assistant", "content": "Checking", "reasoning_content": "trace",
        REPLAY_BINDING: binding.to_payload(), "tool_calls": [{"id": "tool-1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"}, "thought_signature": "signature"}]}
    result: JsonObject = {"role": "tool", "tool_call_id": "tool-1", "content": "source value"}
    kept = transform_replay_messages([row, result], binding, supports_images=False)
    assert kept[0][1] == {key: value for key, value in row.items() if key != REPLAY_BINDING}
    for target in (replace(binding, model_id="other"), replace(binding, protocol="openai-responses"),
                   replace(binding, connection_revision="r2"), replace(binding, connection_id="backup")):
        wire = json.dumps(transform_replay_messages([row, result], target, supports_images=False))
        assert "trace" not in wire and "signature" not in wire
        assert "tool-1" in wire and "source value" in wire
    remapped = transform_replay_messages([row, result, row, result], binding, supports_images=False)
    assert "trace" not in json.dumps(remapped[2:]) and remapped[3][1]["tool_call_id"] != "tool-1"


@pytest.mark.parametrize("same_binding", [True, False])
def test_responses_replay_encodes_only_owned_reasoning_items(same_binding: bool) -> None:
    provider = ApiProvider("http://127.0.0.1:5001/v1", "model", api_protocol="openai-responses")
    binding = provider._binding()
    binding = binding if same_binding else replace(binding, model_id="other")
    reasoning: JsonObject = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    message: dict[str, object] = {"role": "assistant", REPLAY_BINDING: binding.to_payload(),
        RESPONSES_REASONING: [reasoning], "tool_calls": [{"id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"}}]}
    try:
        messages, ids = provider._replayed_view([message, {"role": "tool", "tool_call_id": "call_1", "content": "value"}],
                                               ("entry-call", "entry-result"))
    finally:
        provider.close()
    assert (reasoning in messages) is same_binding
    assert [row["type"] for row in messages if "type" in row] == (
        ["reasoning", "function_call", "function_call_output"] if same_binding else ["function_call", "function_call_output"])
    assert ids[-2:] == ("entry-call", "entry-result") and REPLAY_BINDING not in json.dumps(messages)


@pytest.mark.parametrize("change", ["same", "model", "protocol", "unknown"])
def test_messages_replay_projects_private_blocks_only_to_the_owner(change: str) -> None:
    protocol = "openai-completions" if change == "protocol" else "anthropic-messages"
    provider = ApiProvider("http://127.0.0.1:5001/v1", "model", api_protocol=protocol)
    try:
        target = provider._binding()
        source = replace(target, model_id="other") if change == "model" else replace(target, protocol="anthropic-messages")
        calls = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
        row: dict[str, object] = {"role": "assistant", "content": "Reviewing",
            ANTHROPIC_BLOCKS: [{"type": "thinking", "thinking": "private", "signature": "opaque"},
                               {"type": "text", "text": "Reviewing"},
                               {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {}}],
            "tool_calls": calls}
        if change != "unknown":
            row[REPLAY_BINDING] = source.to_payload()
        view, ids = provider._replayed_view(
            [row, {"role": "tool", "tool_call_id": "call_1", "content": "result"}], ("entry-1", "entry-2"))
        tools = [ProviderToolDefinition("read_file", "read", {"type": "object"})]
        payload = provider._payload(view, tools, GenerationSettings("model", False, 0, None, None, "auto", 100), clock="clock")
    finally:
        provider.close()
    wire = json.dumps(payload)
    assert ("opaque" in wire) is (change == "same")
    assert ANTHROPIC_BLOCKS not in wire and REPLAY_BINDING not in wire
    assert "call_1" in wire and "result" in wire and "Reviewing" in wire
    assert ids == ("entry-1", "entry-2")
