"""Zen owns auxiliary declarations and closure; core sees only ordinary usage/state."""
import time
from unittest.mock import patch

from codey.providers.api_provider import ApiProvider
from codey.providers.zen.connection import ZenProvider


def test_zen_auxiliary_summary_uses_partner_envelope_and_closes_calls_without_execution():
    runtime = ApiProvider("http://localhost:9/v1", "fixture", tool_choice="auto")
    runtime.auxiliary_connection = ZenProvider
    seen, usage = [], []
    runtime.bind_usage("zen", usage.append)

    def generate(url, payload, *args, **kwargs):
        seen.append(payload)
        if len(seen) == 1:
            assert payload.get("tools")
            return {"choices": [{"message": {"tool_calls": [{"id": "refused", "type": "function", "function": {
                "name": payload["tools"][0]["function"]["name"], "arguments": "{}"}}]}}]}
        return {"choices": [{"message": {"content": "Goal: preserve API. Next: read exec-17."}}]}

    with patch("codey.providers.api_transport.generate", side_effect=generate):
        text = runtime._summarize_context([{"role": "user", "content": "Keep API"}], time.monotonic() + 5)
    assert "exec-17" in text
    assert "Not executed" in str(seen[1])
    assert len(usage) == 2 and all(row.purpose == "compaction" for row in usage)
    assert runtime._messages == []


def test_zen_summary_packing_counts_its_actual_prefix_and_required_declarations():
    from codey.providers.token_accounting import RequestContextCount
    counted, sent = [], []

    def counter(payload, *, deadline):
        counted.append(payload)
        return RequestContextCount(len(str(payload.get('messages'))) + (900 if payload.get('tools') else 0), 'tokenizer')

    runtime = ApiProvider('http://localhost:9/v1', 'fixture', context_window_tokens=4096,
                          context_reserve_tokens=1024, context_keep_recent_tokens=1000, request_counter=counter)
    runtime.auxiliary_connection = ZenProvider

    def generate(url, payload, *args, **kwargs):
        sent.append(payload)
        return {'choices':[{'message':{'content':'Goal: preserve API.'}}]}

    with patch('codey.providers.api_transport.generate', side_effect=generate):
        text = runtime._summarize_context([{'role':'user','content':'distinct work ' * 140}], time.monotonic() + 5)
    assert 'preserve API' in text
    assert len(sent) >= 2
    assert all(payload.get('tools') for payload in counted)
    assert all(payload in counted for payload in sent)
