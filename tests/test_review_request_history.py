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
    from types import SimpleNamespace

    from codey.operations.research_review import _submission_executor
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.review_batches import batch_scopes, next_batch
    from tests.durable.test_operation_control_actions import request_for
    from tests.durable.test_provider_response_receipts import runtime
    from tests.test_research_review_batches import large_basis
    from tests.test_research_semantic_review import assessment

    basis = large_basis()
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.write"})),
        task_kind="research_review", task_text="Assess only the frozen report.",
        research_review_basis=basis, max_turns=60)
    payloads = []

    def generate(_url, payload, *_args, **_kwargs):
        payloads.append(copy.deepcopy(payload))
        call = {"tool": "submit_research_review", "args": {"assessment": assessment(next_batch(session, basis))}}
        message = ({"tool_calls": [{"id": f"call-{len(payloads)}", "type": "function",
            "function": {"name": call["tool"], "arguments": json.dumps(call["args"])}}]}
            if native else {"content": json.dumps(call)})
        return {"choices": [{"message": message, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False)
    durable, _, _, sink = runtime(tmp_path, task_kind="research_review", turn_budget=60)
    try:
        submit = _submission_executor(SimpleNamespace(run_id="r"), session, basis, provider_id="local", cycle=0)
        result = run_task_kernel(session, request=request_for(durable, sink, provider,
            executors={"submit_research_review": submit}))
        assert result.stop_reason == "done", result.summary
        review = [row for row in payloads if "FROZEN BASIS:" in json.dumps(row)]
        assert len(review) == len(batch_scopes(basis)) > 4
        for payload in review:
            assert json.dumps(payload).count("FROZEN BASIS:") == 1
            assert len(payload["messages"]) <= 6
            assert payload["max_tokens"] == 2048
        frozen = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "managed_outputs").rglob("*.json")
                  if not path.name.endswith(".meta.json")]
        assert any('"tool_call_history_groups": 2' in json.dumps(row) for row in frozen)
        assert any('"output_token_limit": 2048' in json.dumps(row) for row in frozen)
    finally:
        provider.close()
        durable.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_output_limit_is_counted_sent_and_never_raises_or_mutates_provider_budget(monkeypatch, protocol):
    sent, counted = [], []

    def count(payload, *, deadline):
        counted.append(copy.deepcopy(payload))
        return RequestContextCount(100, "tokenizer")

    def generate(_url, payload, *_args, **_kwargs):
        sent.append(copy.deepcopy(payload))
        if protocol == "openai-completions":
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        return {"output": [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}]}], "status": "completed"}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", api_protocol=protocol,
                           request_counter=count, output_tokens=4096, stream=False)
    try:
        provider.send("bounded", output_token_limit=2048)
        provider.send("cannot raise", output_token_limit=8192)
        provider.send("ordinary")
        key = "max_tokens" if protocol == "openai-completions" else "max_output_tokens"
        assert [row[key] for row in sent] == [2048, 4096, 4096]
        assert counted == sent
        assert provider.context_budget.output_tokens == 4096
        for invalid in (True, False, 0.0, -1, "2048"):
            with pytest.raises(ValueError, match="output token limit"):
                provider.send("invalid", output_token_limit=invalid)
        assert len(sent) == 3
    finally:
        provider.close()


def test_truncated_json_is_not_executed_or_resent(monkeypatch):
    from codey.providers.error_classification import OutputLengthError

    sent = []

    def generate(_url, payload, *_args, **_kwargs):
        sent.append(payload)
        return {"choices": [{"message": {"content": '{"tool":"done","args":{'}, "finish_reason": "length"}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", stream=False)
    try:
        with pytest.raises(OutputLengthError):
            provider.send("research", output_token_limit=2048)
        assert len(sent) == 1 and sent[0]["max_tokens"] == 2048
    finally:
        provider.close()
