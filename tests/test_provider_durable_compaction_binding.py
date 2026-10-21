from __future__ import annotations

import pytest

from codey.durable import CommitConflict, DurableSession, MemoryDurableStore
from codey.providers.api_provider import ApiProvider
from codey.providers.context_checkpoint import ContextRange
from codey.providers.context_ledger import digest
from codey.providers.token_accounting import RequestContextCount


def _provider_with_source():
    durable = DurableSession(MemoryDurableStore())
    durable.create_conversation("chat")
    first = durable.append_message("chat", "user_message", {"text": "early fact"})
    second = durable.append_message("chat", "assistant_message", {"text": "old answer"})
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=1000,
                           context_reserve_tokens=100, context_keep_recent_tokens=100,
                           request_counter=lambda _payload, *, deadline: RequestContextCount(1, "estimated"))
    provider.bind_context("chat")
    provider.bind_durable_context(durable, "chat")
    provider.load_durable_context([
        {"role": "user", "content": "early fact"},
        {"role": "assistant", "content": "old answer"},
    ], source_entry_ids=(first.entry_id, second.entry_id))
    return durable, provider, first, second


def test_compaction_commit_writes_the_canonical_range_edit_before_view_commit():
    durable, provider, first, second = _provider_with_source()
    try:
        snapshot = provider._context_snapshot()
        items = list(provider._messages)
        selected = ContextRange(0, 2, digest(items))
        checkpoint = {"kind": "work_state", "text": "retained early fact and decision"}
        assert provider._commit_context(selected, [{"role": "assistant", "content": "retained early fact and decision"}],
                                        checkpoint, snapshot, None, 10.0)
        assert durable.context_snapshot("chat", token_budget=100).messages == (
            {"role": "assistant", "content": "retained early fact and decision"},)
        assert provider._messages == [{"role": "assistant", "content": "retained early fact and decision"}]
        assert first in durable.entries("chat") and second in durable.entries("chat")
    finally:
        provider.close()
        durable.close()


def test_compaction_source_change_refuses_the_durable_edit_and_keeps_provider_view():
    durable, provider, first, _second = _provider_with_source()
    try:
        snapshot = provider._context_snapshot()
        items = list(provider._messages)
        selected = ContextRange(0, 2, digest(items))
        durable.append_message("chat", "user_message", {"text": "concurrent tail"})
        checkpoint = {"kind": "work_state", "text": "stale summary"}
        try:
            provider._commit_context(selected, [{"role": "assistant", "content": "stale summary"}],
                                     checkpoint, snapshot, None, 10.0)
        except CommitConflict:
            pass
        else:
            raise AssertionError("stale canonical compaction was accepted")
        assert provider._messages == items
        assert first in durable.entries("chat")
    finally:
        provider.close()
        durable.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_durable_sources_follow_semantic_messages_when_codec_expands_tool_calls(protocol):
    durable = DurableSession(MemoryDurableStore())
    durable.create_conversation("chat")
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol)
    try:
        provider.bind_context("chat")
        provider.bind_durable_context(durable, "chat")
        messages = [
            {"role": "user", "content": "read x"},
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                                                        "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
        ]
        ids = tuple(f"entry-{index}" for index in range(len(messages)))
        provider.load_durable_context(messages, source_entry_ids=ids)
        mapped = provider._durable_source_entry_ids
        assert mapped[0] == ids[0]
        assert mapped[-1] == ids[-1]
        assert mapped.count(ids[1]) >= 1
        assert mapped.count(ids[2]) >= 1
    finally:
        provider.close()
        durable.close()
