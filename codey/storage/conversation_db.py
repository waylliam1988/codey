"""Conversation transactions own their SQLite handles, including on Windows."""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def conversation_database(path: Path) -> Generator[sqlite3.Connection, None, None]:
    database = sqlite3.connect(path)
    try:
        database.execute("PRAGMA synchronous=FULL")
        with database:
            yield database
    finally:
        database.close()


def initialize_conversation_database(path: Path, schema: str) -> None:
    with conversation_database(path) as database:
        database.execute("PRAGMA journal_mode=WAL")
        if database.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
            raise ValueError("invalid conversation store version")
        database.execute("PRAGMA user_version=1")
        database.execute(schema)
