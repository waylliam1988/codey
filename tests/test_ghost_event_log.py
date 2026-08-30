from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codey.ghost.event_log import GhostEventLog


class GhostEventLogTests(unittest.TestCase):
    def test_invalid_bad_row_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                GhostEventLog(
                    Path(td) / "events.jsonl",
                    schema_version=1,
                    bad_row_policy="ignore",  # type: ignore[arg-type]
                )

    def test_default_policy_blocks_on_first_bad_row(self) -> None:
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

            self.assertTrue(read.blocked)
            self.assertEqual(read.rows, ())
            self.assertEqual(read.warnings, ("events.jsonl:2:bad_json",))

    def test_warn_policy_reports_bad_rows_without_blocking_valid_prefix(self) -> None:
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
            log = GhostEventLog(
                path,
                schema_version=1,
                source_name="events.jsonl",
                bad_row_policy="warn",
            )

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

    def test_append_rejects_nan_non_object_and_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            log = GhostEventLog(path, schema_version=1, max_bytes=48)

            self.assertFalse(log.append(({"schema_version": 1, "score": float("nan")},)))
            self.assertFalse(log.append(([],)))  # type: ignore[list-item]
            self.assertTrue(log.append(({"schema_version": 1, "type": "ok"},)))
            self.assertFalse(log.append(({"schema_version": 1, "payload": "x" * 200},)))

            read = log.read()

        self.assertFalse(read.blocked)
        self.assertEqual(read.rows, ({"schema_version": 1, "type": "ok"},))

    def test_allowed_event_kinds_are_enforced_on_append_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            log = GhostEventLog(
                path,
                schema_version=1,
                allowed_event_kinds=("expected",),
            )

            self.assertFalse(log.append(({"schema_version": 1, "type": "other"},)))
            self.assertTrue(log.append(({"schema_version": 1, "type": "expected"},)))
            path.write_text(
                path.read_text(encoding="utf-8")
                + '{"schema_version":1,"type":"other"}\n',
                encoding="utf-8",
            )
            read = log.read()

        self.assertTrue(read.blocked)
        self.assertEqual(read.rows, ())
        self.assertEqual(read.warnings, ("events.jsonl:2:unsupported_event",))

    def test_event_validator_is_enforced_on_append_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            log = GhostEventLog(
                path,
                schema_version=1,
                event_validator=lambda event: event.get("ok") is True,
                source_name="events.jsonl",
            )

            self.assertFalse(log.append(({"schema_version": 1, "ok": False},)))
            self.assertTrue(log.append(({"schema_version": 1, "ok": True},)))
            path.write_text(
                path.read_text(encoding="utf-8") + '{"schema_version":1,"ok":false}\n',
                encoding="utf-8",
            )
            read = log.read()

        self.assertTrue(read.blocked)
        self.assertEqual(read.rows, ())
        self.assertEqual(read.warnings, ("events.jsonl:2:invalid_event",))

    def test_quarantine_tail_keeps_valid_prefix_and_blocks_mid_file_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text(
                '{"schema_version":1,"type":"ok"}\nnot json\n',
                encoding="utf-8",
            )
            log = GhostEventLog(
                path,
                schema_version=1,
                source_name="events.jsonl",
                bad_row_policy="quarantine_tail",
            )

            read = log.read()

            self.assertFalse(read.blocked)
            self.assertEqual(read.rows, ({"schema_version": 1, "type": "ok"},))
            self.assertIn("events.jsonl:2:tail_quarantined", read.warnings)
            self.assertTrue(list(Path(td).glob(".events.jsonl.*.corrupt")))

            path.write_text(
                '{"schema_version":1,"type":"ok"}\nnot json\n{"schema_version":1,"type":"later"}\n',
                encoding="utf-8",
            )
            blocked = log.read()

        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.rows, ())
        self.assertIn("events.jsonl:2:mid_file_corruption", blocked.warnings)


if __name__ == "__main__":
    unittest.main()
