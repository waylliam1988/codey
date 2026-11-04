"""Check actual production payloads, not only a bounded display string."""

import copy
import json

import pytest

from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import RequestContextCount
from codey.research.answer_contract import ANSWER_GUIDANCE
from tests.test_research_whole_kernel import TASK, run_whole


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_whole_task_survives_actual_provider_history_trimming_and_repair(tmp_path, monkeypatch, native):
    sent, providers = [], []

    def wrap(scripted):
        def generate(_url, payload, *_args, **_kwargs):
            sent.append(copy.deepcopy(payload))
            prompt = "\n".join(str(row.get("content", "")) for row in payload["messages"])
            raw = scripted.send(prompt)
            row = json.loads(raw)
            message = ({"tool_calls": [{"id": f"call-{len(sent)}", "type": "function", "function": {
                "name": row["tool"], "arguments": json.dumps(row["args"])}}]} if native else {"content": raw})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        monkeypatch.setattr(api_generation, "generate", generate)
        provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False,
                               request_counter=lambda *a, **kw: RequestContextCount(100, "tokenizer"))
        providers.append(provider)
        return provider

    try:
        result = run_whole(tmp_path, native=native, scenario="repair", provider_wrap=wrap)
        assert result.stop_reason == "done" and len(sent) == 3
        for payload in sent:
            actual = "\n".join(str(row.get("content", "")) for row in payload["messages"])
            assert TASK in actual and actual.count(ANSWER_GUIDANCE) == 1
            assert "Register 1..8 questions with research_plan" not in actual
            assert "Submit only report_sections.coverage" not in actual
            assert "submit_research_answer" in actual
            if native:
                assert payload["tool_choice"] == "required"
        assert "[e1]" in json.dumps(sent[1]) and "e99" in json.dumps(sent[2])
        actual_read = "\n".join(str(row.get("content", "")) for row in sent[1]["messages"])
        assert actual_read.count("Readers and a writer may run concurrently. Only one writer at a time.") == 1
        assert "passage_ref=p" not in actual_read
        assert actual_read.index("Readers and a writer may run concurrently.") < actual_read.index("Complete user task (verbatim):")
    finally:
        for provider in providers:
            provider.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_complete_answer_is_frozen_in_bounded_payload_and_protocol_pairs_survive(tmp_path, monkeypatch, native):
    sent, counted, providers = [], [], []

    def factory(scripted):
        def generate(_url, payload, *_args, **_kwargs):
            sent.append(copy.deepcopy(payload))
            prompt = "\n".join(str(row.get("content", "")) for row in payload["messages"])
            raw = scripted.send(prompt)
            row = json.loads(raw)
            message = ({"tool_calls": [{"id": f"call-{len(sent)}", "type": "function", "function": {
                "name": row["tool"], "arguments": json.dumps(row["args"])}}]} if native else {"content": raw})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        def count(payload, *, deadline):
            counted.append(copy.deepcopy(payload))
            return RequestContextCount(100, "tokenizer")

        monkeypatch.setattr(api_generation, "generate", generate)
        provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False, request_counter=count)
        providers.append(provider)
        return provider

    try:
        result = run_whole(tmp_path, native=native, provider_wrap=factory)
        assert result.stop_reason == "done", result.summary
        assert counted and all(any(payload == measured for measured in counted) for payload in sent)
        scopes = [payload for payload in sent if "Complete user task (verbatim):" in json.dumps(payload)]
        assert len(scopes) == 2
        for payload in scopes:
            assert json.dumps(payload).count("Complete user task (verbatim):") == 1
            assert len(payload["messages"]) <= 7
            assert TASK in json.dumps(payload)  # original constraints anchor remains
            if native:
                ids = [item["id"] for message in payload["messages"] for item in message.get("tool_calls", [])]
                returned = [message["tool_call_id"] for message in payload["messages"] if "tool_call_id" in message]
                assert ids == returned
        frozen = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "managed_outputs").rglob("*.json")
                  if not path.name.endswith(".meta.json")]
        assert any('"tool_call_history_groups": 1' in json.dumps(row) and '"research_scope"' in json.dumps(row) for row in frozen)
        assert "Only one writer can write at a time" in result.summary
    finally:
        for provider in providers:
            provider.close()


@pytest.mark.parametrize("groups", [1, 5, 10])
@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_old_work_does_not_grow_sent_input_while_or_tail_and_anchor_remain(monkeypatch, groups, protocol):
    sent = []

    def generate(_url, payload, *_args, **_kwargs):
        sent.append(payload)
        return ({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
                if protocol == "openai-completions" else {"output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}]}], "status": "completed"})

    monkeypatch.setattr(api_generation, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", api_protocol=protocol, stream=False,
                           request_counter=lambda *args, **kwargs: RequestContextCount(100, "tokenizer"))
    history = []
    for n in range(groups):
        history.extend([{"role": "user", "content": f"UNRELATED-SECRET-{n}" + "x" * 10000},
                        {"role": "assistant", "content": "old answer"}])
    try:
        provider.load_durable_context(history, source_entry_ids=tuple(f"entry-{n}" for n in range(len(history))))
        before = copy.deepcopy(provider._messages)
        provider.send("Current target", tool_call_history_groups=1,
                      tool_call_observations="Original task constraint: do not invent measurements. A OR B OR FINAL_ALTERNATIVE_C.")
        actual = json.dumps(sent[0])
        assert "UNRELATED-SECRET" not in actual and "FINAL_ALTERNATIVE_C" in actual
        assert "do not invent measurements" in actual and len(actual) < 2000
        assert provider._messages[:len(before)] == before
    finally:
        provider.close()

