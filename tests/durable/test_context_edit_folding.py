"""Committed range transforms fold in order without rewriting their source facts."""
from __future__ import annotations

import json
from contextlib import closing
from io import BytesIO
from types import SimpleNamespace

import pytest

from codey.agents.handoff import ConversationContext
from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.conversation_window import activate_window
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.providers.token_accounting import RequestContextCount


@pytest.fixture(params=["memory", "sqlite"])
def session(request, tmp_path):
    durable = DurableSession(MemoryDurableStore() if request.param == "memory"
                             else SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    durable.create_conversation("chat")
    try:
        yield durable
    finally:
        durable.close()


def edit(durable, request_id, edits, conversation="chat"):
    head, revision = durable.head(conversation)
    return durable.admit_write(conversation_id=conversation, request_id=request_id, kind="context_edit", data={},
                               edits=edits, display_only=False, expected_revision=revision, expected_head=head)


def replacement(target, text):
    return {"target": target, "action": "replace", "messages": [{"role": "assistant", "content": text}]}


def seed(durable):
    first = durable.append_message("chat", "user_message", {"text": "early fact " * 1000})
    source = durable.append_message("chat", "assistant_message", {"text": "old execution result"})
    tail = durable.append_message("chat", "user_message", {"text": "latest constraint"})
    edit(durable, "first-summary", [replacement(first.entry_id, "retained early fact"),
                                   {"target": source.entry_id, "action": "omit"}])
    return first, source, tail


def test_next_checkpoint_can_replace_a_previous_checkpoint(session):
    first, source, tail = seed(session)
    snapshot = session.context_snapshot("chat", token_budget=100)
    edit(session, "second-summary", [replacement(snapshot.entry_ids[0], "retained early fact and new decision")])
    folded = session.context_snapshot("chat", token_budget=100)
    assert folded.messages == ({"role": "assistant", "content": "retained early fact and new decision"},
                               {"role": "user", "content": "latest constraint"})
    assert folded.entry_ids[0] == f"{first.entry_id}:replacement:0:replacement:0"
    assert all(entry in session.entries("chat") for entry in (first, source, tail))
    assert source.entry_id in folded.omitted_entry_ids


def test_omitting_a_checkpoint_does_not_resurrect_its_raw_sources(session):
    seed(session)
    snapshot = session.context_snapshot("chat", token_budget=100)
    edit(session, "omit-summary", [{"target": snapshot.entry_ids[0], "action": "omit"}])
    assert session.context_snapshot("chat", token_budget=100).messages == (
        {"role": "user", "content": "latest constraint"},)


@pytest.mark.parametrize("action", ["omit", "replace"])
def test_editing_the_original_anchor_overrides_its_whole_replacement_tree(session, action):
    first, _, _ = seed(session)
    edit(session, "nested-summary", [replacement(f"{first.entry_id}:replacement:0", "nested summary")])
    edit(session, "override-anchor", [replacement(first.entry_id, "corrected summary") if action == "replace"
                                      else {"target": first.entry_id, "action": "omit"}])
    messages = session.context_snapshot("chat", token_budget=100).messages
    assert messages == (({"role": "assistant", "content": "corrected summary"},) if action == "replace" else ()) + (
        {"role": "user", "content": "latest constraint"},)


def test_child_checkpoint_folds_only_its_capped_ancestor_transform(session):
    first, _, _ = seed(session)
    parent_snapshot = session.context_snapshot("chat", token_budget=100)
    cap = session.head("chat")[0]
    child = session.fork("chat", cap, child_conversation_id="child")
    edit(session, "parent-later", [replacement(first.entry_id, "later parent correction")])
    edit(session, "child-summary", [replacement(parent_snapshot.entry_ids[0], "child decision")], conversation=child)
    assert session.context_snapshot(child, token_budget=100).messages == (
        {"role": "assistant", "content": "child decision"}, {"role": "user", "content": "latest constraint"})
    assert session.context_snapshot("chat", token_budget=100).messages == (
        {"role": "assistant", "content": "later parent correction"}, {"role": "user", "content": "latest constraint"})


def test_selecting_a_branch_before_the_edits_restores_original_facts(session):
    first, source, tail = seed(session)
    edit(session, "nested-summary", [replacement(f"{first.entry_id}:replacement:0", "nested summary")])
    session.branch("chat", tail.entry_id, expected_revision=session.head("chat")[1])
    original = session.context_snapshot("chat", token_budget=10000)
    assert original.entry_ids == (first.entry_id, source.entry_id, tail.entry_id)
    assert original.messages[0]["content"] == "early fact " * 1000


def test_nested_checkpoint_and_new_tail_rebuild_identically_after_restart(tmp_path):
    path = tmp_path / "durable.sqlite3"
    with closing(DurableSession(SQLiteDurableStore(path))) as durable:
        durable.create_conversation("chat")
        first, _, _ = seed(durable)
        edit(durable, "nested-summary", [replacement(f"{first.entry_id}:replacement:0", "retained early fact and decision")])
        durable.append_message("chat", "assistant_message", {"text": "new incremental answer"})
        expected = durable.context_snapshot("chat", token_budget=100)
    with closing(DurableSession(SQLiteDurableStore(path))) as durable:
        assert durable.context_snapshot("chat", token_budget=100) == expected
        assert expected.messages == (
            {"role": "assistant", "content": "retained early fact and decision"},
            {"role": "user", "content": "latest constraint"},
            {"role": "assistant", "content": "new incremental answer"})


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_folded_checkpoint_and_native_tail_reach_the_actual_codec_attempt(session, monkeypatch, protocol):
    from codey.providers import api_transport

    first, _, _ = seed(session)
    edit(session, "nested-summary", [replacement(f"{first.entry_id}:replacement:0", "retained early fact and decision")])
    session.append_message("chat", "tool_call", {"tool_calls": [
        {"id": "tail-call", "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
    session.append_message("chat", "tool_result", {"call_id": "tail-call", "content": "latest receipt"})
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol, native_tools=True)
    counted, sent = [], []

    def count(payload, *, deadline):
        counted.append(json.loads(json.dumps(payload)))
        return RequestContextCount(100, "tokenizer")

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    def send(request, timeout):
        sent.append(json.loads(request.data))
        body = ({"choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]}
                if protocol == "openai-completions" else {"status": "completed", "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}]})
        return Response(json.dumps(body).encode())

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", send)
    state = SimpleNamespace(durable=session, provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="local", provider=provider,
        conversation=ConversationContext(), task_kind="chat", project=None, task="next", continue_task=False,
        trace=None, pure=True)
    activate_window(provider, plan.window_action)
    assert provider.send_turn("next", [ProviderToolDefinition("read", "read file", {})]).text == "answer"
    assert counted[-1] == sent[0]
    items = sent[0]["messages" if protocol == "openai-completions" else "input"]
    assert items[0] == {"role": "assistant", "content": "retained early fact and decision"}
    assert items[1] == {"role": "user", "content": "latest constraint"}
    assert items[-1] == {"role": "user", "content": "next"}
    assert json.dumps(items).count("tail-call") == 2
    assert "old execution result" not in json.dumps(items)
    provider.close()
