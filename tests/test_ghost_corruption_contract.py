"""Ghost SQLite failures must fail closed without a JSONL compatibility path."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.ghost.observations import GhostObservationStore
from codey.ghost.sqlite import GhostSchemaVersionError


class GhostSQLiteContractTests(unittest.TestCase):
    def test_newer_schema_is_rejected_on_cold_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ghost.sqlite3"
            database = sqlite3.connect(path)
            try:
                database.execute("PRAGMA user_version=2")
                database.commit()
            finally:
                database.close()
            with self.assertRaises(GhostSchemaVersionError):
                GhostObservationStore(Path(td))

    def test_append_failure_does_not_claim_a_committed_observation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = GhostObservationStore(Path(td))
            with mock.patch.object(
                store.database,
                "connection",
                side_effect=sqlite3.OperationalError("injected ghost commit failure"),
            ):
                self.assertFalse(store.append_completed(
                    run_id="r1", session_id="s", mode="chat", user_text="hi",
                    assistant_text="hello", stop_reason="done", provider_id="local",
                ))
            self.assertEqual(store.read_all(), ())


if __name__ == "__main__":
    unittest.main()
