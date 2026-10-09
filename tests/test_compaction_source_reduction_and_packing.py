"""Summary requests preserve distinct facts while avoiding redundant source work."""
import time
from unittest.mock import patch

from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import RequestContextCount


def test_repeated_source_lines_are_counted_once_without_losing_boundary_facts():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    source = [{"role": "tool", "content": "Constraint: no-db\n" + "same observation\n" * 650 + "Exit code: 1; result exec-17"}]
    sent = []
    def generate(url, payload, *args, **kwargs):
        sent.append(str(payload))
        return {"choices": [{"message": {"content": "Goal: preserve no-db. Exit code: 1, exec-17"}}]}
    with patch("codey.providers.api_transport.generate", side_effect=generate):
        provider._summarize_context(source, time.monotonic() + 10)
    assert len(sent) == 1
    assert len(sent[0]) < 2000
    assert "650" in sent[0] and "no-db" in sent[0] and "exec-17" in sent[0]
    assert source[0]["content"].count("same observation") == 650


def test_chunk_admission_uses_actual_counter_instead_of_a_character_ratio():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=2000)
    provider.request_counter = lambda payload, **kwargs: RequestContextCount(len(str(payload)) // 20, "tokenizer")
    sent = []
    def generate(url, payload, *args, **kwargs):
        sent.append(payload)
        return {"choices": [{"message": {"content": "Goal: preserve facts"}}]}
    with patch("codey.providers.api_transport.generate", side_effect=generate):
        provider._summarize_context([{"role": "assistant", "content": "abcdefghijklmnop " * 1200}], time.monotonic() + 10)
    assert len(sent) == 1
