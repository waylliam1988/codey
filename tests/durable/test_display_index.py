from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.durable import DurableMutation, DurableSession, EntryDraft, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import SchemaVersionError, SessionSealedError
from codey.durable.history import HistoryEntry, history_path
from codey.durable.history_index import NodeBatch, insert, size, window
from codey.durable.model import TaskCheckpoint
from codey.durable.sqlite_history import SQLiteHistoryReader


@pytest.fixture(params=["memory", "sqlite"])
def session(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[DurableSession]:
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "history.sqlite")
    durable = DurableSession(store)
    durable.create_conversation("chat")
    try:
        yield durable
    finally:
        durable.close()


def test_persistent_tree_balances_both_directions_and_preserves_old_roots() -> None:
    batch = NodeBatch(MemoryDurableStore())
    keys = list(range(300))
    random.Random(19).shuffle(keys)
    root: str | None = None
    retained: list[tuple[str, list[int]]] = []
    expected: list[int] = []
    for key in keys:
        root = insert(batch, root, f"{key:04d}", str(key), question=key % 3 == 0)
        expected = sorted([*expected, key])
        if len(expected) % 50 == 0:
            retained.append((root, expected))
    for old_root, values in retained:
        assert [int(node.value) for node in window(batch, old_root, 0, len(values))] == values
        questions = [key for key in values if key % 3 == 0]
        assert [int(node.value) for node in window(batch, old_root, 3, 12, questions=True)] == questions[3:12]
        assert size(batch, old_root, questions=True) == len(questions)

    def check(identity: str | None) -> int:
        if identity is None:
            return 0
        node = batch.node(identity)
        left, right = check(node.left), check(node.right)
        assert abs(left - right) <= 1
        assert node.height == max(left, right) + 1
        return node.height

    check(root)
    nodes = len(batch.nodes)
    assert insert(batch, root, "0019", "19", question=False) == root
    assert len(batch.nodes) == nodes


def assert_matches_reference(session: DurableSession, conversation: str) -> None:
    with session.store.history_snapshot() as reader:
        head, _ = reader.head(conversation)
        expected = history_path(reader, conversation, head)
    items: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        page = session.history_page(conversation, before=cursor, limit=3)
        items = page["items"] + items
        if not page["has_more"]:
            break
        cursor = page["before_cursor"]
    assert [item["id"] for item in items] == [entry.entry_id for entry in expected]
    assert [item["ordinal"] for item in items] == list(range(1, len(expected) + 1))
    assert [item["id"] for item in session.navigation_page(conversation)["items"]] == [
        entry.entry_id for entry in expected if entry.kind == "user_message"
    ]
    for entry in expected:
        assert entry.entry_id in {item["id"] for item in session.history_window(conversation, entry.entry_id, limit=3)["items"]}


def test_projection_matches_reference_across_hidden_caps_markers_and_nested_forks(session: DurableSession) -> None:
    first = session.append_message("chat", "user_message", {"text": "one"})
    hidden = session.append_message("chat", "control_config", {"secret": "never display"})
    session.append_message("chat", "assistant_message", {"text": "two"})
    session.fork("chat", hidden.entry_id, child_conversation_id="child")
    session.append_message("child", "user_message", {"text": "child"})
    session.fork("child", first.entry_id, child_conversation_id="grandchild")
    session.append_message("grandchild", "user_message", {"text": "grandchild"})
    for conversation in ("chat", "child", "grandchild"):
        assert_matches_reference(session, conversation)
    old_cursor = session.history_page("child", limit=1)["before_cursor"]
    session.append_message("child", "control_config", {"head_marker": True})
    session.append_message("child", "user_message", {"text": "after cut"})
    assert_matches_reference(session, "child")
    assert_matches_reference(session, "grandchild")
    with pytest.raises(ValueError, match="history snapshot changed"):
        session.history_page("child", before=old_cursor)
    # The explicit retained hidden ancestor is still an admissible window tip.
    assert session.history_window("child", first.entry_id, leaf_id=hidden.entry_id)["items"][0]["id"] == first.entry_id


def test_visible_task_set_uses_any_visible_record_and_rejects_stale_validation(session: DurableSession) -> None:
    session.admit_input(conversation_id="chat", request_id="input", task_id="run", text="input", provider_snapshot={})
    head, revision = session.store.head("chat")
    marker = EntryDraft("cut", "chat", head, "control_config", {"head_marker": True})
    reply = EntryDraft("reply", "chat", "cut", "assistant_message", {"text": "still visible"}, by_task_id="run")
    session.store.commit(DurableMutation("chat", revision, entries=(marker, reply)))
    page = session.history_page("chat")
    assert [item["id"] for item in page["items"]] == ["reply"]
    result = session.validate_history_runs("chat", ["run", "unknown"], epoch=page["epoch"], generation=page["path_generation"])
    assert result["runs"] == [{"run_id": "run", "visible": True}, {"run_id": "unknown", "visible": False}]
    session.append_message("chat", "control_receipt", {"head_marker": True})
    with pytest.raises(ValueError, match="history snapshot changed"):
        session.validate_history_runs("chat", ["run"], epoch=page["epoch"], generation=page["path_generation"])
    empty = session.history_page("chat")
    assert empty["items"] == []
    assert session.validate_history_runs("chat", ["run"], epoch=empty["epoch"], generation=empty["path_generation"])["runs"] == [
        {"run_id": "run", "visible": False},
    ]
    with pytest.raises(ValueError, match="200"):
        session.validate_history_runs("chat", [str(index) for index in range(201)], epoch=empty["epoch"],
                                      generation=empty["path_generation"])


def test_cursor_types_tampering_branch_versions_and_database_identity(session: DurableSession, tmp_path: Path) -> None:
    for index in range(5):
        session.append_message("chat", "user_message", {"text": str(index)})
    page = session.history_page("chat", limit=2)
    cursor = page["before_cursor"]
    with pytest.raises(ValueError, match="cursor"):
        session.navigation_page("chat", before=cursor)
    with pytest.raises(ValueError, match="cursor"):
        session.history_page("chat", before=cursor[:-1] + ("0" if cursor[-1] != "0" else "1"))
    other = DurableSession(SQLiteDurableStore(tmp_path / "other.sqlite"))
    try:
        other.create_conversation("chat")
        with pytest.raises(ValueError, match="cursor"):
            other.history_page("chat", before=cursor)
    finally:
        other.close()
    # Even selecting the same head is an explicit path rewrite under the new contract.
    session.branch("chat", session.store.head("chat")[0])
    with pytest.raises(ValueError, match="history snapshot changed"):
        session.history_page("chat", before=cursor)


def test_inherited_edits_keep_existing_descendants_on_their_fixed_versions(session: DurableSession) -> None:
    sources: list[str] = []
    for run in ("first", "second"):
        session.admit_input(conversation_id="chat", request_id=run, task_id=run, text=run, provider_snapshot={})
        sources.append(str(session.task(run).state["input_entry_id"]))
        session.complete_task(run, f"answer:{run}", state={})
    session.fork("chat", session.store.head("chat")[0], child_conversation_id="child")
    session.fork("child", sources[1], child_conversation_id="grandchild")
    retained = session.history_page("grandchild")["items"]
    session.admit_input(conversation_id="child", request_id="child-edit", task_id="child-edit", text="replacement",
                        provider_snapshot={}, task_state={"input": {"kind": "edit_user_message", "text": "replacement",
                            "entry_id": sources[1], "expected_revision": session.store.head("child")[1]}})
    session.complete_task("child-edit", "child answer", state={})
    session.admit_input(conversation_id="chat", request_id="parent-edit", task_id="parent-edit", text="new root",
                        provider_snapshot={}, task_state={"input": {"kind": "edit_user_message", "text": "new root",
                            "entry_id": sources[0], "expected_revision": session.store.head("chat")[1]}})
    for conversation in ("chat", "child", "grandchild"):
        assert_matches_reference(session, conversation)
    assert session.history_page("grandchild")["items"] == retained
    assert [item["text"] for item in session.navigation_page("child")["items"]] == ["first", "replacement"]


def test_many_forks_share_the_prefix_without_copying_entries_or_index_nodes(tmp_path: Path) -> None:
    store = SQLiteDurableStore(tmp_path / "forks.sqlite")
    session = DurableSession(store)
    session.create_conversation("chat")
    try:
        for index in range(100):
            session.append_message("chat", "user_message", {"text": str(index)})
        tip = store.head("chat")[0]
        with closing(store._connect()) as database:
            original_nodes = int(database.execute("SELECT COUNT(*) FROM display_nodes").fetchone()[0])
        for index in range(32):
            session.fork("chat", tip, child_conversation_id=f"child-{index}")
        with closing(store._connect()) as database:
            assert database.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 100
            assert database.execute("SELECT COUNT(*) FROM display_nodes").fetchone()[0] == original_nodes
        for index in range(32):
            session.append_message(f"child-{index}", "user_message", {"text": f"child {index}"})
        with closing(store._connect()) as database:
            assert database.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 132
            assert database.execute("SELECT COUNT(*) FROM display_nodes").fetchone()[0] < original_nodes + 32 * 50
        assert [item["text"] for item in session.history_page("child-31", limit=3)["items"]] == ["98", "99", "child 31"]
    finally:
        session.close()


def test_sqlite_index_rolls_back_with_original_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.durable import sqlite
    from codey.durable.sqlite_display import write_display_index

    store = SQLiteDurableStore(tmp_path / "rollback.sqlite")
    session = DurableSession(store)
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "before"})
    head, revision = store.head("chat")
    sequence = store.commit_sequence()

    def fail_after_projection(database: sqlite3.Connection, mutation: DurableMutation, entries: tuple[EntryDraft, ...]) -> None:
        write_display_index(database, mutation, entries)
        raise OSError("injected index failure")

    monkeypatch.setattr(sqlite, "write_display_index", fail_after_projection)
    with pytest.raises(OSError, match="injected"):
        store.commit(DurableMutation("chat", revision, entries=(EntryDraft("failed", "chat", head, "user_message", {}),)))
    assert store.head("chat") == (head, revision)
    assert store.commit_sequence() == sequence
    with closing(store._connect()) as database:
        assert database.execute("SELECT 1 FROM entries WHERE id='failed'").fetchone() is None
        assert database.execute("SELECT 1 FROM display_versions WHERE entry_id='failed'").fetchone() is None
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [item["text"] for item in session.history_page("chat")["items"]] == ["before"]
    session.close()


def test_reopen_preserves_cursor_roots_and_leaf_deletion_preserves_sibling_versions(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite"
    session = DurableSession(SQLiteDurableStore(path))
    session.create_conversation("chat")
    for index in range(10):
        session.append_message("chat", "user_message", {"text": str(index)})
    page = session.history_page("chat", limit=2)
    tip = session.store.head("chat")[0]
    for child in ("left", "right"):
        session.fork("chat", tip, child_conversation_id=child)
        session.append_message(child, "user_message", {"text": child})
    session.close()
    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        assert [item["text"] for item in reopened.history_page("chat", before=page["before_cursor"], limit=2)["items"]] == ["6", "7"]
        reopened.delete_conversation("left")
        assert_matches_reference(reopened, "right")
        with closing(reopened.store._connect()) as database:
            assert database.execute("SELECT COUNT(*) FROM display_nodes WHERE owner='left'").fetchone()[0] == 0
            assert database.execute("SELECT COUNT(*) FROM display_nodes WHERE owner='chat'").fetchone()[0] > 0
    finally:
        reopened.close()


@pytest.mark.parametrize("query", ["page", "window", "navigation"])
def test_paused_snapshot_does_not_block_same_session_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           query: str) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "parallel.sqlite"))
    session.create_conversation("chat")
    entry = session.append_message("chat", "user_message", {"text": "snapshot"})
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []
    pages: list[dict[str, object]] = []

    def pause() -> None:
        entered.set()
        assert release.wait(10)

    original_select = SQLiteHistoryReader.select_entries
    original_nav = SQLiteHistoryReader.navigation_items

    def select(reader: SQLiteHistoryReader, entries: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]:
        pause()
        return original_select(reader, entries)

    def navigation(reader: SQLiteHistoryReader, identities: list[str]) -> list[dict[str, object]]:
        pause()
        return original_nav(reader, identities)

    monkeypatch.setattr(SQLiteHistoryReader, "navigation_items" if query == "navigation" else "select_entries",
                        navigation if query == "navigation" else select)

    def read() -> None:
        try:
            if query == "page":
                pages.append(session.history_page("chat"))
            elif query == "window":
                pages.append(session.history_window("chat", entry.entry_id))
            else:
                pages.append(session.navigation_page("chat"))
        except BaseException as error:
            errors.append(error)

    def write() -> None:
        try:
            session.append_message("chat", "user_message", {"text": "new commit"})
            written.set()
        except BaseException as error:
            errors.append(error)

    reader, writer = threading.Thread(target=read), threading.Thread(target=write)
    try:
        reader.start()
        assert entered.wait(5)
        writer.start()
        assert written.wait(5), "same-session writer was blocked by history read"
    finally:
        release.set()
        reader.join(10)
        if writer.ident is not None:
            writer.join(10)
        session.close()
    assert not errors
    assert [item["text"] for item in pages[0]["items"]] == ["snapshot"]


def test_close_waits_for_snapshot_without_owning_session_write_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "close.sqlite"))
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "read"})
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = SQLiteHistoryReader.select_entries
    errors: list[BaseException] = []

    def select(reader: SQLiteHistoryReader, selected: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]:
        entered.set()
        assert release.wait(5)
        return original(reader, selected)

    def read() -> None:
        try:
            session.history_page("chat")
        except BaseException as error:
            errors.append(error)

    def close() -> None:
        session.close()
        closed.set()

    monkeypatch.setattr(SQLiteHistoryReader, "select_entries", select)
    reader, closer = threading.Thread(target=read), threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert not closed.wait(.1)
        with session.control_line():
            assert not closed.is_set()
    finally:
        release.set()
        reader.join(5)
        if closer.ident is not None:
            closer.join(5)
        session.close()
    assert closed.is_set() and not errors
    with pytest.raises(SessionSealedError):
        session.history_page("chat")


@dataclass
class SQLWork:
    index_rows: int = 0
    instructions: int = 0


class MeasuredStore(SQLiteDurableStore):
    def __init__(self, path: Path) -> None:
        self.work: SQLWork | None = None
        super().__init__(path, synchronous="NORMAL")

    def _connect(self) -> sqlite3.Connection:
        database = super()._connect()
        work = self.work
        if work is not None:
            def trace(statement: str) -> None:
                if statement.startswith("SELECT * FROM display_nodes WHERE id="):
                    work.index_rows += 1

            def progress() -> int:
                work.instructions += 100
                return 0

            database.set_trace_callback(trace)
            database.set_progress_handler(progress, 100)
        return database

    @contextmanager
    def measure(self) -> Generator[SQLWork]:
        self.work = SQLWork()
        try:
            yield self.work
        finally:
            self.work = None


def test_read_work_grows_logarithmically_at_100_1000_10000_messages_and_tasks(tmp_path: Path) -> None:
    store = MeasuredStore(tmp_path / "work.sqlite")
    session = DurableSession(store)
    session.admit_input(conversation_id="chat", request_id="seed", task_id="seed", text="0", provider_snapshot={})
    submission_id = session.task("seed").submission_id
    previous = 1
    report: list[dict[str, object]] = []
    try:
        for count in (100, 1000, 10000):
            head, revision = store.head("chat")
            entries: list[EntryDraft] = []
            tasks: list[TaskCheckpoint] = []
            for index in range(previous, count):
                identity = f"entry-{index}"
                run = f"run-{index}"
                entries.append(EntryDraft(identity, "chat", head, "user_message", {"text": str(index)}, by_task_id=run))
                tasks.append(TaskCheckpoint(run, "chat", submission_id, "accepted", 0, {}, {}))
                head = identity
            started = time.perf_counter()
            store.commit(DurableMutation("chat", revision, entries=tuple(entries), task_updates=tuple(tasks)))
            write_seconds = time.perf_counter() - started
            with store.measure() as page_work:
                page = session.history_page("chat", limit=10)
            assert len(page["items"]) == 10
            with store.measure() as earlier_work:
                session.history_page("chat", before=page["before_cursor"], limit=10)
            with store.measure() as window_work:
                session.history_window("chat", f"entry-{count // 2}", limit=10)
            with store.measure() as navigation_work:
                session.navigation_page("chat", limit=10)
            with store.measure() as run_work:
                result = session.validate_history_runs("chat", [f"run-{count // 2}", "absent"],
                                                       epoch=page["epoch"], generation=page["path_generation"])
            assert result["runs"] == [{"run_id": f"run-{count // 2}", "visible": True},
                                       {"run_id": "absent", "visible": False}]
            with store.measure() as reference_work, store.history_snapshot() as reader:
                reference_rows = len(history_path(reader, "chat", reader.head("chat")[0]))
            assert reference_rows == count
            for work in (page_work, earlier_work, window_work, navigation_work, run_work):
                assert work.index_rows <= 12 * math.ceil(math.log2(count)) + 40
                assert work.instructions < 15000
            with closing(store._connect()) as database:
                nodes = int(database.execute("SELECT COUNT(*) FROM display_nodes").fetchone()[0])
                pages = int(database.execute("PRAGMA page_count").fetchone()[0])
                page_size = int(database.execute("PRAGMA page_size").fetchone()[0])
            report.append({"messages": count, "index_nodes": nodes, "database_bytes": pages * page_size,
                           "append_seconds": round(write_seconds, 4), "appended_messages": count - previous,
                           "page_index_rows": page_work.index_rows, "page_vm_steps": page_work.instructions,
                           "earlier_index_rows": earlier_work.index_rows,
                           "window_index_rows": window_work.index_rows,
                           "navigation_index_rows": navigation_work.index_rows,
                           "task_index_rows": run_work.index_rows,
                           "reference_metadata_rows": reference_rows, "reference_vm_steps": reference_work.instructions})
            previous = count
        assert report[-1]["page_index_rows"] < 4 * report[0]["page_index_rows"]
        assert report[-1]["task_index_rows"] < 4 * report[0]["task_index_rows"]
        print("\nHISTORY_INDEX_WORK=" + json.dumps(report, sort_keys=True))
    finally:
        session.close()


def test_previous_cold_start_shape_is_rejected_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "previous.sqlite"
    SQLiteDurableStore(path)
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE display_versions")
        database.execute("DROP TABLE display_nodes")
        identity = database.execute("SELECT value FROM schema_metadata WHERE key='database_id'").fetchone()
    with pytest.raises(SchemaVersionError, match="schema"):
        SQLiteDurableStore(path)
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT value FROM schema_metadata WHERE key='database_id'").fetchone() == identity
        assert database.execute("SELECT name FROM sqlite_master WHERE name='display_nodes'").fetchone() is None
