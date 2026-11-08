"""Bounded body reads without changing fork, cursor or snapshot semantics."""

import base64
import json
import threading
from unittest.mock import Mock

import pytest

from codey.durable import DurableMutation, DurableSession, EntryDraft, MemoryDurableStore, SQLiteDurableStore


@pytest.fixture(params=["memory", "sqlite"])
def durable(request, tmp_path):
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "history.sqlite3")
    session = DurableSession(store)
    session.create_conversation("chat")
    yield session
    session.close()


def test_sqlite_decodes_only_the_requested_page_and_never_loads_all_entries(tmp_path, monkeypatch):
    from codey.durable import sqlite, sqlite_history

    store = SQLiteDurableStore(tmp_path / "history.sqlite3")
    session = DurableSession(store)
    try:
        session.create_conversation("chat")
        entries = tuple(EntryDraft(str(index), "chat", str(index - 1) if index else None,
                                   "user_message", {"text": f"{index}:" + "x" * 1024}) for index in range(1000))
        store.commit(DurableMutation("chat", 0, entries=entries))
        decoded = Mock(wraps=sqlite.entry_from_row)
        monkeypatch.setattr(sqlite, "entry_from_row", decoded)
        monkeypatch.setattr(sqlite_history, "entry_from_row", decoded)
        monkeypatch.setattr(store, "entries", Mock(side_effect=AssertionError("unbounded body read")))
        page = session.history_page("chat", limit=10)
        assert [item["id"] for item in page["items"]] == [str(index) for index in range(990, 1000)]
        assert [item["ordinal"] for item in page["items"]] == list(range(991, 1001))
        assert decoded.call_count == 10
        page = session.history_page("chat", before=page["before_cursor"], limit=10)
        assert [item["id"] for item in page["items"]] == [str(index) for index in range(980, 990)]
        assert decoded.call_count == 20
    finally:
        session.close()


def test_sqlite_page_finishes_before_another_session_deletes_the_chat(tmp_path, monkeypatch):
    from codey.durable.sqlite_history import SQLiteHistoryReader
    from codey.durable.task_projection import task_lifecycle

    path = tmp_path / "snapshot.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    writer = DurableSession(SQLiteDurableStore(path))
    reading = threading.Event()
    release = threading.Event()
    deleted = threading.Event()
    pages = []
    errors = []
    try:
        session.admit_input(conversation_id="chat", request_id="r", task_id="task", text="snapshot", provider_snapshot={})
        revision = session.store.head("chat")[1]
        expected_task = task_lifecycle(session.task("task"))
        select = SQLiteHistoryReader.select_entries

        def pause_before_loading_bodies(reader, selected):
            reading.set()
            assert release.wait(5)
            return select(reader, selected)

        def read():
            try:
                pages.append(session.history_page("chat", limit=1))
            except BaseException as error:
                errors.append(error)

        def delete():
            try:
                writer.delete_conversation("chat")
                deleted.set()
            except BaseException as error:
                errors.append(error)

        monkeypatch.setattr(SQLiteHistoryReader, "select_entries", pause_before_loading_bodies)
        reader_thread = threading.Thread(target=read)
        reader_thread.start()
        assert reading.wait(5)
        deleting_thread = threading.Thread(target=delete)
        deleting_thread.start()
        assert not deleted.wait(.1)
        release.set()
        reader_thread.join(5)
        deleting_thread.join(5)
        assert deleted.is_set()
        assert not errors
        page = pages[0]
        assert page["snapshot_revision"] == revision
        assert [item["text"] for item in page["items"]] == ["snapshot"]
        assert page["task_states"] == [expected_task]
        assert not session.has_conversation("chat")
    finally:
        release.set()
        writer.close()
        session.close()


def test_paging_nested_forks_edit_rewinds_head_markers_and_rejects_unsigned_cursors(durable):
    first = durable.append_message("chat", "user_message", {"text": "one"})
    second = durable.append_message("chat", "assistant_message", {"text": "two"})
    durable.fork("chat", second.entry_id, child_conversation_id="child")
    durable.append_message("child", "user_message", {"text": "excluded child"})
    durable.fork("child", first.entry_id, child_conversation_id="grandchild")
    durable.append_message("grandchild", "user_message", {"text": "three"})
    page = durable.history_page("grandchild", limit=1)
    legacy = base64.urlsafe_b64encode(json.dumps({"s": "grandchild", "e": page["items"][0]["id"]}).encode()).decode()
    with pytest.raises(ValueError, match="cursor"):
        durable.history_page("grandchild", before=legacy, limit=1)
    older = durable.history_page("grandchild", before=page["before_cursor"], limit=1)
    assert [item["text"] for item in older["items"] + page["items"]] == ["one", "three"]
    with pytest.raises(ValueError, match="cursor"):
        durable.history_page("child", before=page["before_cursor"])
    with pytest.raises(ValueError, match="cursor"):
        durable.history_page("grandchild", before="invalid cursor")

    marker = durable.append_message("grandchild", "display_message", {"text": "new head", "head_marker": True})
    durable.append_message("grandchild", "control_config", {"private": "excluded"})
    durable.append_message("grandchild", "context_snapshot", {"private": "excluded"})
    durable.append_message("grandchild", "user_message", {"text": "four"})
    items = durable.history_page("grandchild")["items"]
    assert [item["text"] for item in items] == ["new head", "four"]
    assert items[0]["id"] == marker.entry_id
    assert [item["ordinal"] for item in items] == [1, 2]


def test_page_after_editing_an_inherited_message_matches_the_context(durable):
    for name in ("first", "second"):
        durable.admit_input(conversation_id="chat", request_id=name, task_id=name, text=name, provider_snapshot={})
        durable.complete_task(name, f"answer {name}", state={})
    durable.fork("chat", durable.store.head("chat")[0], child_conversation_id="child")
    source = durable.task("second").state["input_entry_id"]
    durable.admit_input(conversation_id="child", request_id="edit", task_id="edit", text="replacement",
                        provider_snapshot={}, task_state={"input": {"kind": "edit_user_message", "text": "replacement",
                            "entry_id": source, "expected_revision": durable.store.head("child")[1]}})
    items = durable.history_page("child")["items"]
    assert [item["text"] for item in items] == ["first", "answer first", "replacement"]
    assert [item["id"] for item in items] == [entry.entry_id for entry in durable.context("child")]


@pytest.mark.parametrize("sqlite", [False, True])
def test_artifacts_are_only_resolved_when_their_entry_is_in_the_page(tmp_path, sqlite):
    from pathlib import Path

    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from tests.durable.test_provider_response_receipts import runtime
    from tests.support.provider_actions import ProviderActions

    session, _, _, sink = runtime(tmp_path, sqlite=sqlite)
    try:
        task = TaskSession(TaskPolicy(frozenset({"project.read", "control"})), max_turns=4)
        sink.bind_request(build_turn_snapshot(task, native=True), logical_turn=1)
        reply = AssistantTurn(text="tool body", tool_calls=(ProviderToolCall("one", "read_file", {"path": "a.py"}),))
        ProviderActions(Mock(window_ref=None, native_tools=False, send=Mock(return_value=reply)), sink).send("inspect")
        ref = operation_from_checkpoint(session.task("r"), "s").response_ref
        session.append_message("s", "display_message", {"text": "latest"})
        Path(session.artifact(ref).storage_path).write_text("corrupted", encoding="utf-8")
        page = session.history_page("s", limit=1)
        assert page["items"][0]["text"] == "latest"
        with pytest.raises(ValueError, match="digest"):
            session.history_page("s", before=page["before_cursor"], limit=1)
    finally:
        session.close()
