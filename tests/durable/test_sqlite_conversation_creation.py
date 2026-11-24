import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

import pytest

from codey.durable import CommitConflict, SQLiteDurableStore


def test_concurrent_conversation_creation_returns_the_same_identity(tmp_path: Path) -> None:
    store = SQLiteDurableStore(tmp_path / "durable.sqlite3")
    inserts = Barrier(2)

    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO conversations"):
                # A serialized writer reaches INSERT only once.
                with suppress(BrokenBarrierError):
                    inserts.wait(timeout=2)
            if not isinstance(parameters, tuple):
                raise TypeError("the conversation fixture expects positional SQL parameters")
            arguments: tuple[object, ...] = parameters
            return super().execute(sql, arguments)

    def make_row(cursor: sqlite3.Cursor, values: tuple[object, ...]) -> sqlite3.Row:
        return sqlite3.Row(cursor, values)

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(store.path, isolation_level=None, timeout=10, factory=Connection)
        connection.row_factory = make_row
        return connection

    with patch.object(store, "_connect", new=connect), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.create_conversation, "conversation")
        second = pool.submit(store.create_conversation, "conversation")
        first.result(timeout=15)
        second.result(timeout=15)

    assert store.conversation_ids() == ("conversation",)
    assert store.head("conversation") == (None, 0)
    with pytest.raises(CommitConflict, match="different ancestry"):
        store.create_conversation("conversation", parent_conversation_id="other")
