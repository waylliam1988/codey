"""Context views commit transactionally while original accepted events remain readable."""
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider


def test_provider_context_is_bound_to_the_durable_runtime_window():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.bind_context("chat-a")
    with patch("codey.providers.api_transport.generate", return_value={"choices": [{"message": {"content": "answer"}}]}):
        provider.send("remember this exact original")
    assert provider.context_ledger.events()[0]["content"] == "remember this exact original"
    other = ApiProvider("http://localhost:9/v1", "fixture")
    other.bind_context("chat-a")
    assert other.context_ledger.events() == []
    assert other._messages == []


def test_session_and_protocol_ledger_factories_share_identity(tmp_path):
    from codey.providers.context_ledger import ContextLedger
    from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding

    session_id = "chat-factory"
    first = ContextLedger.for_session(tmp_path, session_id, "model")
    ref = ProtocolWindowRef.new(session_id, ProviderBinding("local", "r", "model", "openai-completions", "contract"))
    first.open_window(ref, reset_permit=True)
    reopened = ContextLedger.for_protocol_session(tmp_path, session_id, ref)
    assert reopened.identity == f"protocol:{session_id}"


def test_new_chat_starts_an_empty_provider_window():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.bind_context("chat-a")
    with patch("codey.providers.api_transport.generate", return_value={"choices": [{"message": {"content": "answer"}}]}):
        provider.send("original")
    provider.new_chat()
    assert provider.context_ledger.events()
    assert provider._messages == []


def test_failed_generation_does_not_archive_unconfirmed_user_input():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.bind_context("chat-a")
    with patch("codey.providers.api_transport.generate", side_effect=RuntimeError("offline")), pytest.raises(RuntimeError):
        provider.send("not accepted")
    assert provider.context_ledger.events() == []


def test_two_writers_cannot_overwrite_an_already_committed_archive(tmp_path):
    from codey.providers.context_ledger import ContextLedger

    first = ContextLedger.for_session(tmp_path, "a", "model")
    second = ContextLedger.for_session(tmp_path, "a", "model")
    original = [{"role": "user", "content": "first"}]
    first.commit(original, events=original)
    with pytest.raises(ValueError, match="changed"):
        second.commit([{"role": "user", "content": "second"}])
    assert ContextLedger.for_session(tmp_path, "a", "model").events() == original


def test_corrupted_archive_fails_closed(tmp_path):
    from codey.providers.context_ledger import ContextLedger

    ledger = ContextLedger.for_session(tmp_path, "a", "model")
    ledger.commit([], events=[{"role": "user", "content": "original"}])
    assert ledger.root
    (ledger.root / "000000000001.json").write_text('{"sequence":1,"previous":"","events":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        ContextLedger.for_session(tmp_path, "a", "model")


def test_close_does_not_create_an_independent_recovery_archive(tmp_path):
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.bind_context("a")
    with patch("codey.providers.api_transport.generate", return_value={"choices": [{"message": {"content": "answer"}}]}):
        provider.send("original")
    expected = list(provider._messages)
    provider.close()
    other = ApiProvider("http://localhost:9/v1", "fixture")
    other.bind_context("a")
    assert other._messages == []
    assert expected


def test_malformed_native_reply_preserves_valid_input_durably_without_archiving_bad_calls(tmp_path):
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.bind_context("a")
    original = [{"role": "user", "content": "accepted work"}]
    provider.context_ledger.commit(original, events=original)
    provider._messages = original
    malformed = {"choices": [{"message": {"tool_calls": [
        {"id": "bad", "function": {"name": "run", "arguments": "{"}}]}}]}
    with patch("codey.providers.api_transport.generate", return_value=malformed):
        provider.send_turn("continue")
    expected = [*original, {"role": "user", "content": "continue"}]
    assert provider._messages == expected
    other = ApiProvider("http://localhost:9/v1", "fixture")
    other.bind_context("a")
    assert other._messages == []
    assert other.context_ledger.events() == []


def test_emergency_multi_segment_compaction_commits_all_lineage_with_the_answer():
    from codey.providers.context_ledger import digest
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=1024,
                           context_reserve_tokens=256, context_keep_recent_tokens=60)
    first = provider._codec.checkpoint_item("Old state A")
    second = provider._codec.checkpoint_item("Old state B")
    history = [first, {"role": "assistant", "content": "alpha " * 1500},
               second, {"role": "assistant", "content": "beta " * 1500}, {"role": "user", "content": "latest"}]
    for item in (first, second):
        provider.context_ledger.commit(history, checkpoint={"kind":"work_state", "item_digest":digest(item), "source":[]})
    provider._messages = history
    provider.summarize_context = lambda source, _: "Goal: keep API"
    with patch("codey.providers.api_transport.generate", return_value={"choices": [{"message": {"content": "ok"}}]}):
        provider.send("continue")
    assert len(provider.context_ledger.checkpoint_digests()) == 4
    assert "alpha" in str(provider.context_ledger.original_source(provider._messages))
    assert "beta" in str(provider.context_ledger.original_source(provider._messages))
    staged = [row for row in provider._compaction.diagnostics if row.get("kind") == "work_state"]
    assert len(staged) == 2
    assert all(row["committed"] for row in staged)
