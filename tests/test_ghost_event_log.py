from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codey.ghost.event_log import GhostEventLog


class GhostEventLogTests(unittest.TestCase):
    def test_read_reports_bad_rows_without_blocking_valid_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text(
                "\n".join((
                    '{"schema_version":1,"type":"ok"}',
                    "not json",
                    "[]",
                    '{"schema_version":2,"type":"old"}',
                )),
                encoding="utf-8",
            )
            log = GhostEventLog(path, schema_version=1, source_name="events.jsonl")

            read = log.read()

            self.assertFalse(read.blocked)
            self.assertEqual(read.rows, ({"schema_version": 1, "type": "ok"},))
            self.assertEqual(read.warnings, (
                "events.jsonl:2:bad_json",
                "events.jsonl:3:not_object",
                "events.jsonl:4:unsupported_schema",
            ))

    def test_max_bytes_blocks_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text('{"schema_version":1,"type":"ok"}\n', encoding="utf-8")
            log = GhostEventLog(path, schema_version=1, max_bytes=4, source_name="events.jsonl")

            read = log.read()

            self.assertTrue(read.blocked)
            self.assertEqual(read.rows, ())
            self.assertEqual(read.warnings, ("events.jsonl:too_large",))

    def test_append_prune_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            log = GhostEventLog(path, schema_version=1)

            self.assertTrue(log.append(
                {"schema_version": 1, "index": index}
                for index in range(4)
            ))
            log.prune_tail(2)

            self.assertEqual(
                log.read().rows,
                (
                    {"schema_version": 1, "index": 2},
                    {"schema_version": 1, "index": 3},
                ),
            )

            log.delete()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

