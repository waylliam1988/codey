"""Temporary Zen declarations satisfy transport without changing task authority."""
import json
from unittest.mock import Mock

import pytest

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition
from codey.providers.zen.connection import ZenProvider


def response(text="", *, calls=()):
    return {"status": "completed", "output": [
        *({"type": "function_call", "status": "completed", "call_id": identity,
           "name": name, "arguments": json.dumps(args)} for identity, name, args in calls),
        *([{"type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text}]}] if text else []),
    ]}


def provider_with_responses(monkeypatch, replies, protocol="openai-responses"):
    requests = []
    replies = iter(replies)

    def generate(endpoint, payload, headers, **kwargs):
        requests.append(payload)
        # Reproduce the observed service requirement, without a network call.
        names = {tool.get("name", tool.get("function", {}).get("name")) for tool in payload.get("tools", [])}
        if not {"read", "shell"} <= names:
            raise api_transport.GenerationRejectedError(403, '{"error":{"type":"FreeTierError"}}')
        assert payload["tool_choice"] == "auto"
        body = next(replies)
        if protocol == "openai-completions":
            message = {"content": "", "tool_calls": []}
            for item in body["output"]:
                if item["type"] == "message":
                    message["content"] += item["content"][0]["text"]
                elif item["type"] == "function_call":
                    message["tool_calls"].append({"id": item["call_id"], "type": "function",
                        "function": {"name": item["name"], "arguments": item["arguments"]}})
            return {"choices": [{"message": message, "finish_reason": "tool_calls" if message["tool_calls"] else "stop"}]}
        return body

    monkeypatch.setattr(api_transport, "generate", generate)
    runtime = ApiProvider("http://fixture.test/v1", "fixture", api_protocol=protocol, tool_choice="auto")
    return ZenProvider(runtime), requests


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_plain_review_returns_original_json_with_one_generation_and_no_execution(monkeypatch, protocol):
    text = '{"verdict":"approved","summary":"Checked","findings":[]}'
    provider, requests = provider_with_responses(monkeypatch, [response(text)], protocol)
    assert provider.send("Review only", timeout=3) == text
    assert len(requests) == 1
    wire = [t.get("function", t) for t in requests[0]["tools"]]
    assert {t["name"] for t in wire} == {"read", "shell"}
    assert all("unavailable" in t["description"].lower() for t in wire)


@pytest.mark.parametrize("grants, name, args", [
    ({"control"}, "read", {"path": "secret.txt"}),
    ({"control", "project.read"}, "shell", {"command": "echo unsafe > marker.txt"}),
])
def test_real_kernel_refuses_supplemental_calls_with_original_ids_and_original_policy(tmp_path, monkeypatch, grants, name, args):
    marker = tmp_path / "marker.txt"
    marker.write_text("original", encoding="utf8")
    provider, requests = provider_with_responses(monkeypatch, [
        response(calls=[("exact-id", name, args)]),
        response(calls=[("finish-id", "done", {"summary": "No commands executed."})]),
        response("Acknowledged"),
    ])
    policy = TaskPolicy(frozenset(grants))
    session = TaskSession(policy=policy, task_kind="planning_readonly", project=str(tmp_path), max_turns=4)
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=provider, user_task="Do not execute commands or change files."),
        execution=KernelExecutionDeps(project_path=tmp_path)))
    assert result.completed, result.summary
    assert session.policy == policy
    assert marker.read_text(encoding="utf8") == "original"
    settled = [item for q in requests for item in q["input"] if item.get("type") == "function_call_output"]
    refused = [item for item in settled if item["call_id"] == "exact-id"]
    assert refused and all("not" in item["output"].lower() or "denied" in item["output"].lower() for item in refused)


def test_plain_tool_calls_receive_explicit_refusal_without_running_any_tool(monkeypatch):
    provider, requests = provider_with_responses(monkeypatch, [
        response(calls=[("plain-call", "shell", {"command": "dangerous"})]), response("Review answer"),
    ])
    assert provider.send("Review only", timeout=3) == "Review answer"
    assert len(requests) == 2
    outputs = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
    assert outputs == [{"type": "function_call_output", "call_id": "plain-call",
                        "output": "Not executed: tools are unavailable for this text-only request. Reply with text only."}]


def test_plain_tool_refusal_is_bounded_and_does_not_fabricate_text_success(monkeypatch):
    provider, requests = provider_with_responses(monkeypatch, [
        response(calls=[(f"call-{i}", "read", {"path": "private"})]) for i in range(3)
    ])
    with pytest.raises(RuntimeError, match="text-only"):
        provider.send("Review only", timeout=3)
    assert len(requests) == 3
    assert not provider.has_transport_history


def test_alias_collision_is_rejected_before_any_generation(monkeypatch):
    generate = Mock()
    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ZenProvider(ApiProvider("http://fixture.test/v1", "fixture"))
    tools = [ProviderToolDefinition(name, "description", {}) for name in ("read_file", "read")]
    with pytest.raises(ValueError, match="collision"):
        provider.send_turn("Read", tools)
    generate.assert_not_called()


def test_close_between_plain_reply_and_refusal_delivery_does_not_start_another_request():
    runtime = Mock()
    provider = ZenProvider(runtime)

    def reply(*args, **kwargs):
        provider.close()
        return AssistantTurn(tool_calls=(ProviderToolCall("old", "read", {"path": "x"}),))

    runtime.send_turn.side_effect = reply
    with pytest.raises(api_transport.GenerationUnknownError):
        provider.send("Review", timeout=1)
    runtime.acknowledge_tool_results.assert_not_called()


def test_transport_revision_changes_review_identity_without_changing_runtime_identity():
    runtime = ApiProvider("http://fixture.test/v1", "fixture")
    provider = ZenProvider(runtime)
    assert provider.model_identity != runtime.model_identity
    assert ZenProvider(runtime).model_identity == provider.model_identity


def test_actual_transport_declaration_change_invalidates_review_identity(monkeypatch):
    from codey.providers.zen import declarations

    provider = ZenProvider(ApiProvider("http://fixture.test/v1", "fixture"))
    before = provider.model_identity
    monkeypatch.setattr(declarations, "_UNAVAILABLE", "Changed transport description. ")
    assert provider.model_identity != before


def test_plain_reasoning_is_preserved_only_for_the_accepted_reply():
    runtime = Mock()
    runtime.send_turn.return_value = AssistantTurn(text="Answer", reasoning="Reasoned answer")
    provider = ZenProvider(runtime)
    assert provider.send("Discuss", timeout=3) == "Answer"
    assert provider.normalize_reply("Answer") == AssistantTurn(text="Answer", reasoning="Reasoned answer")
    assert provider.normalize_reply("Different") == "Different"
    provider.close()
    assert provider.normalize_reply("Answer") == "Answer"


def test_text_channel_output_limit_is_not_a_successful_review(monkeypatch):
    from codey.providers.error_classification import OutputLengthError

    body = response("truncated")
    body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    succeeded = Mock()
    provider, requests = provider_with_responses(monkeypatch, [body])
    provider.on_plain_succeeded = succeeded
    with pytest.raises(OutputLengthError):
        provider.send("Review", timeout=3)
    assert len(requests) == 1 and not provider.has_transport_history
    succeeded.assert_not_called()


def test_overlapping_plain_and_native_sends_fail_fast_without_interleaving(monkeypatch):
    runtime = Mock()
    provider = ZenProvider(runtime)

    def reply(*args, **kwargs):
        with pytest.raises(RuntimeError, match="busy"):
            provider.send_turn("Concurrent", [])
        return AssistantTurn(text="Original")

    runtime.send_turn.side_effect = reply
    assert provider.send("Review", timeout=3) == "Original"
    assert runtime.send_turn.call_count == 1
