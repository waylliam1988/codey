"""Isolated production databases under the test session's temporary root."""
from pathlib import Path
from tempfile import mkdtemp

from codey.durable.sqlite import SQLiteDurableStore


class TemporarySQLiteStore(SQLiteDurableStore):
    def __init__(self) -> None:
        super().__init__(Path(mkdtemp(prefix="codey-durable-")) / "durable.sqlite3")
