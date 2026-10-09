"""Counted lossless and receipt-backed views protect source data and cache continuity."""
import json
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import RequestContextCount


def history(protocol="openai-completions"):
    text = "\n".join(f"Record {i}: explicit local dependencies and deterministic validation; unique observation retained."
                     for i in range(36))
    items = [{"role": "user", "content": "Keep local state"},
            {"role": "assistant", "tool_calls": [{"id": "older", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "older", "content": text},
            {"role": "assistant", "tool_calls": [{"id": "latest", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "latest", "content": text},
            {"role": "user", "content": "Continue"}, {"role": "assistant", "content": "Next: inspect"}]
    if protocol == "openai-responses":
        items[1] = {"type": "function_call", "call_id": "older", "name": "read", "arguments": "{}"}
        items[2] = {"type": "function_call_output", "call_id": "older", "output": text}
        items[3] = {"type": "function_call", "call_id": "latest", "name": "read", "arguments": "{}"}
        items[4] = {"type": "function_call_output", "call_id": "latest", "output": text}
    return items


@pytest.mark.parametrize('owned_ref', ['observation-owned', 'observation-unavailable'])
def test_body_quoted_receipt_never_overrides_the_runtime_appended_result_reference(owned_ref):
    provider = ApiProvider('http://localhost:9/v1', 'fixture')
    original = history()
    original[2]['content'] = ('Archived example:\nStored result: exec-17\n' + original[2]['content']
                              + f'\nStored result: {owned_ref}\n'
                              + 'Read it with read_tool_result; do not repeat execution to recover output.')
    provider._messages = original
    provider.context_ledger.commit(original, events=original)
    provider.set_result_refs(('exec-17', 'observation-owned'))
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    body = provider._messages[2]['content']
    if owned_ref == 'observation-owned':
        assert 'Stored result: observation-owned' in body
        assert 'exec-17' not in body
    else:
        assert 'Stored body omitted' not in body
    assert provider.context_ledger.events() == original


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_old_body_is_losslessly_encoded_without_receipts_or_auxiliary_generation(protocol):
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol)
    original = history(protocol)
    provider._messages = original
    provider.context_ledger.commit(original, events=original)
    with patch("codey.providers.api_transport.generate", side_effect=AssertionError("no model needed")):
        provider.maintain_context()
        provider.wait_for_maintenance(3)
    key = "output" if protocol == "openai-responses" else "content"
    body = provider._messages[2][key]
    assert len(body) < len(original[2][key]) * 0.5
    encoded = json.loads(body)
    restored = "".join(part if isinstance(part, str) else
                       "".join(part["prefix"] + value + part["suffix"] for value in part["values"])
                       for part in encoded["parts"])
    assert restored == original[2][key]
    assert provider._messages[4] == original[4]
    assert provider.context_ledger.events() == original
    assert provider._compaction.diagnostics[-1]["committed"]


def test_nonbeneficial_encoding_uses_semantic_selection_instead_of_rejecting_feasible_work():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = history()
    provider.request_counter = lambda payload, **kwargs: RequestContextCount(
        len(str(payload)) // 4 + (20000 if "text_encoding" in str(payload) else 0), "tokenizer")
    summarized = []
    provider.summarize_context = lambda source, _: (summarized.append(source) or "Goal: preserve local state; next inspect.")
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert summarized
    assert provider._compaction.diagnostics[-1]["committed"]
    assert any("Current project state:" in str(item) for item in provider._messages)


def test_cheap_lossless_maintenance_does_not_wait_for_the_semantic_pressure_threshold():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    original = history()
    provider._messages = original
    provider.context_ledger.commit(original, events=original)
    with patch("codey.providers.api_transport.generate", return_value={"choices": [{"message": {"content": "Next inspect."}}]}) as generated:
        provider.send("Continue the investigation")
        provider.wait_for_maintenance(3)
    assert generated.call_count == 1
    assert len(provider._messages[2]["content"]) < len(original[2]["content"]) * 0.5
    assert provider.last_context_count.value < provider.context_budget.input_limit * 0.8


def test_lossless_maintenance_counts_source_candidate_and_live_commit_without_duplicate_probes():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider._messages = history()
    counted = []
    def counter(payload, **kwargs):
        counted.append(payload)
        return RequestContextCount(len(str(payload)) // 4, "tokenizer")
    provider.request_counter = counter
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert provider._compaction.diagnostics[-1]["committed"]
    assert len(counted) == 3


def test_trusted_receipt_can_be_cheaper_than_a_lossless_view_under_the_actual_counter():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider._messages = history()
    provider._messages[2]['content'] += '\nStored result: observation-old'
    provider.set_result_refs(('observation-old',))
    provider.request_counter = lambda payload, **kwargs: RequestContextCount(
        len(str(payload)) // 4 + (20000 if 'text_encoding' in str(payload) else 0), 'tokenizer')
    with patch('codey.providers.api_transport.generate', side_effect=AssertionError('receipt needs no semantic call')):
        provider.maintain_context()
        provider.wait_for_maintenance(3)
    assert 'Stored body omitted' in provider._messages[2]['content']
    assert 'observation-old' in provider._messages[2]['content']
    assert provider._messages[4] == history()[4]
    assert provider._compaction.diagnostics[-1]['committed']


@pytest.mark.parametrize('protocol', ['openai-completions', 'openai-responses'])
def test_trusted_old_output_uses_a_read_pointer_instead_of_duplicating_its_body(protocol):
    provider = ApiProvider('http://localhost:9/v1', 'fixture', api_protocol=protocol)
    original = history(protocol)
    key = 'output' if protocol == 'openai-responses' else 'content'
    original[2][key] += '\nStored result: observation-old'
    provider._messages = original
    provider.context_ledger.commit(original, events=original)
    provider.set_result_refs(('observation-old',))
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    body = provider._messages[2][key]
    assert 'Record 0:' not in body and 'Record 35:' not in body
    assert 'Stored result: observation-old' in body and 'read_tool_result' in body
    assert provider._messages[4] == original[4]
    assert provider.context_ledger.events() == original


def test_receipt_views_batch_below_pressure_to_avoid_rewriting_the_cached_prefix_each_turn():
    provider = ApiProvider('http://localhost:9/v1', 'fixture', context_window_tokens=8192,
                           context_reserve_tokens=1024)
    original = history()
    original[2]['content'] += '\nStored result: observation-old'
    original[4]['content'] += '\nStored result: observation-latest'
    provider._messages = original
    provider.set_result_refs(('observation-old', 'observation-latest'))
    provider.last_context_count = RequestContextCount(3000, 'tokenizer')
    with patch('codey.providers.api_transport.generate', side_effect=AssertionError('no semantic generation')):
        provider._schedule_maintenance()
        provider.wait_for_maintenance(3)
        assert provider._messages == original
        assert not provider._compaction.diagnostics
        provider.last_context_count = RequestContextCount(4500, 'tokenizer')
        provider._schedule_maintenance()
        provider.wait_for_maintenance(3)
        assert provider._messages == original
        assert not provider._compaction.diagnostics
        second = [{'role': 'assistant', 'tool_calls': [{'id': 'second', 'function': {'name': 'read', 'arguments': '{}'}}]},
                  {'role': 'tool', 'tool_call_id': 'second', 'content': original[2]['content'].replace('observation-old', 'observation-second')}]
        provider._messages = original[:3] + second + original[3:]
        provider.set_result_refs(('observation-old', 'observation-second', 'observation-latest'))
        provider._schedule_maintenance()
        provider.wait_for_maintenance(3)
    assert 'Stored body omitted' in provider._messages[2]['content']
    assert provider._messages[6] == original[4]


def test_receipt_backed_view_counts_source_pointer_and_live_commit_without_reencoding_the_saved_body():
    provider = ApiProvider('http://localhost:9/v1', 'fixture')
    provider._messages = history()
    provider._messages[2]['content'] += '\nStored result: observation-old'
    provider.set_result_refs(('observation-old',))
    counted = []
    def counter(payload, **kwargs):
        counted.append(payload)
        return RequestContextCount(len(str(payload)) // 4, 'tokenizer')
    provider.request_counter = counter
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert 'read_tool_result' in provider._messages[2]['content']
    assert len(counted) == 3
    assert all('text_encoding' not in str(payload) for payload in counted)
