"""Responses compaction retains complete call/results and encrypted reasoning."""
import pytest

from codey.providers.api_responses import prepare
from codey.providers.error_classification import ContextOverflowError


def test_compaction_drops_old_exchange_as_a_whole_without_mutating_history():
    old = [{"role": "user", "content": "old " * 3000},
           {"type": "reasoning", "encrypted_content": "old opaque"},
           {"type": "function_call", "call_id": "old", "name": "read", "arguments": "{}"},
           {"type": "function_call_output", "call_id": "old", "output": "old result"}]
    recent = [{"role": "user", "content": "current task"},
              {"type": "reasoning", "encrypted_content": "new opaque"},
              {"type": "function_call", "call_id": "new", "name": "read", "arguments": "{}"}]
    result = {"type": "function_call_output", "call_id": "new", "output": "current result"}
    history = old + recent
    compacted = prepare(history, [result], system="", tools=[], budget=(1500, 300, 900))
    assert compacted == recent + [result]
    assert history == old + recent


def test_latest_exchange_overflow_refuses_before_send():
    with pytest.raises(ContextOverflowError):
        prepare([], [{"role": "user", "content": "huge " * 5000}], system="", tools=[], budget=(1500, 300, 900))
