"""Append durability: ledger/event/session tails survive with fsync."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.storage.atomic_io import append_bytes_durable


class AppendDurableTests(unittest.TestCase):
    def test_append_fsyncs_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log.jsonl"
            path.write_bytes(b"")
            with mock.patch("codey.storage.atomic_io.os.fsync") as fsync:
                append_bytes_durable(path, [b'{"a":1}\n', b'{"a":2}\n'])
            self.assertTrue(fsync.called)
            self.assertEqual(path.read_bytes(), b'{"a":1}\n{"a":2}\n')

    def test_run_ledger_uses_durable_append(self) -> None:
        from codey.runs.ledger import RunLedgerWriter

        with tempfile.TemporaryDirectory() as td:
            with mock.patch(
                "codey.storage.atomic_io.append_bytes_durable",
                wraps=append_bytes_durable,
            ) as durable:
                writer = RunLedgerWriter(
                    Path(td) / "ledger.jsonl", run_id="run-1", session_id="session-1"
                )
                writer.append("info", text="hello")
            self.assertTrue(durable.called)

    def test_ghost_event_log_uses_durable_append(self) -> None:
        from codey.ghost.event_log import GhostEventLog

        with tempfile.TemporaryDirectory() as td:
            log = GhostEventLog(
                Path(td) / "events.jsonl",
                source_name="test",
                schema_version=1,
            )
            with mock.patch(
                "codey.storage.atomic_io.append_bytes_durable",
                wraps=append_bytes_durable,
            ) as durable:
                self.assertTrue(log.append([{"schema_version": 1}]))
            self.assertTrue(durable.called)




if __name__ == "__main__":
    unittest.main()
