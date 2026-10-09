"""Responses compaction retains complete call/results and encrypted reasoning."""
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolResult
from codey.providers.error_classification import ContextOverflowError


def test_checkpoint_keeps_recent_complete_exchanges_without_mutating_history():
    old = [{"role": "user", "content": "old " * 3000},
           {"type": "reasoning", "encrypted_content": "old opaque"},
           {"type": "function_call", "call_id": "old", "name": "read", "arguments": "{}"},
           {"type": "function_call_output", "call_id": "old", "output": "old result"}]
    recent = [{"role": "user", "content": "current task"},
              {"type": "reasoning", "encrypted_content": "new opaque"},
              {"type": "function_call", "call_id": "new", "name": "read", "arguments": "{}"}]
    result = {"type": "function_call_output", "call_id": "new", "output": "current result"}
    history = old + recent
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol="openai-responses",
                           context_window_tokens=1500, context_reserve_tokens=300, context_keep_recent_tokens=900)
    provider._messages = history
    provider.summarize_context = lambda *_: "Goal: preserve original work"
    with patch("codey.providers.api_transport.generate", return_value={"status": "completed", "output": []}):
        provider.send_tool_results([ProviderToolResult("new", "current result")], [])
    compacted = provider._messages
    assert compacted[1:] == old[1:] + recent + [result]
    assert "Goal: preserve original work" in str(compacted[0])
    assert history == old + recent


def test_latest_exchange_overflow_refuses_before_send():
    with pytest.raises(ContextOverflowError):
        provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol="openai-responses",
                               context_window_tokens=1500, context_reserve_tokens=300, context_keep_recent_tokens=900)
        with patch("codey.providers.api_transport.open_request", side_effect=AssertionError("must not send")):
            provider.send("huge " * 5000)
