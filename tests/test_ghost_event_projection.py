"""Ghost event_projection: shared projection-file mechanics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.ghost.event_projection import over_compact_budget, read_projection_payload


def _payload(**overrides: object) -> dict:
    base: dict[str, object] = {"schema_version": 1, "kind": "test_projection"}
    base.update(overrides)
    return base


class ReadProjectionPayloadTests(unittest.TestCase):
    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload, reason = read_projection_payload(
                Path(td) / "absent.json", schema_version=1, kind="test_projection", max_bytes=1024
            )
        self.assertIsNone(payload)
        self.assertEqual(reason, "missing")

    def test_valid_payload_passes_through(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps(_payload(nodes=[])), encoding="utf-8")
            payload, reason = read_projection_payload(
                path, schema_version=1, kind="test_projection", max_bytes=1024
            )
        self.assertEqual(reason, "")
        self.assertEqual(payload, _payload(nodes=[]))

    def test_garbage_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_bytes(b"\x00\x01 not json {{{")
            payload, reason = read_projection_payload(
                path, schema_version=1, kind="test_projection", max_bytes=1024
            )
        self.assertIsNone(payload)
        self.assertEqual(reason, "corrupt")

    def test_non_dict_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            payload, reason = read_projection_payload(
                path, schema_version=1, kind="test_projection", max_bytes=1024
            )
        self.assertIsNone(payload)
        self.assertEqual(reason, "corrupt")

    def test_oversize_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps(_payload(pad="x" * 2048)), encoding="utf-8")
            payload, reason = read_projection_payload(
                path, schema_version=1, kind="test_projection", max_bytes=64
            )
        self.assertIsNone(payload)
        self.assertEqual(reason, "corrupt")

    def test_schema_or_kind_drift_is_wrong_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old = Path(td) / "old.json"
            old.write_text(json.dumps(_payload(schema_version=0)), encoding="utf-8")
            foreign = Path(td) / "foreign.json"
            foreign.write_text(json.dumps(_payload(kind="other_kind")), encoding="utf-8")
            _, old_reason = read_projection_payload(
                old, schema_version=1, kind="test_projection", max_bytes=4096
            )
            _, foreign_reason = read_projection_payload(
                foreign, schema_version=1, kind="test_projection", max_bytes=4096
            )
        self.assertEqual(old_reason, "wrong_schema")
        self.assertEqual(foreign_reason, "wrong_schema")


class OverCompactBudgetTests(unittest.TestCase):
    def test_at_budget_is_not_over(self) -> None:
        self.assertFalse(
            over_compact_budget({"events": 100, "bytes": 1000}, max_events=100, max_bytes=1000)
        )

    def test_either_dimension_over_is_over(self) -> None:
        self.assertTrue(
            over_compact_budget({"events": 101, "bytes": 1000}, max_events=100, max_bytes=1000)
        )
        self.assertTrue(
            over_compact_budget({"events": 100, "bytes": 1001}, max_events=100, max_bytes=1000)
        )

    def test_garbage_stats_fail_closed(self) -> None:
        # No usage recorded compacts nothing; unparseable counters compact
        # defensively rather than trusting a broken reading.
        self.assertFalse(over_compact_budget({}, max_events=100, max_bytes=1000))
        self.assertTrue(
            over_compact_budget({"events": "many", "bytes": None}, max_events=100, max_bytes=1000)
        )


if __name__ == "__main__":
    unittest.main()
