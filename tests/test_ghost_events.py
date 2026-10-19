from __future__ import annotations

import unittest

from codey.ghost.events import compact_result_payload, control_event, over_compact_budget


class GhostEventEnvelopeTests(unittest.TestCase):
    def test_compact_result_payload_and_control_event_are_schema_neutral(self) -> None:
        before = {"events": "3", "bytes": 40}
        after = {"events": 1, "bytes": "20"}

        payload = compact_result_payload(
            True,
            True,
            before,
            after,
            ("a", "a", "b"),
            warning_cleaner=lambda rows: tuple(dict.fromkeys(rows)),
        )
        control = control_event(
            schema_version=7,
            event_name="ghost_events_compacted",
            payload={"records": 3},
            now="2026-09-06T00:00:00Z",
            event_field="kind",
            timestamp_field="created_at",
            event_id_prefix="gec_",
        )

        self.assertEqual(payload["events_before"], 3)
        self.assertEqual(payload["bytes_after"], 20)
        self.assertEqual(payload["warnings"], ["a", "b"])
        self.assertEqual(control["schema_version"], 7)
        self.assertEqual(control["kind"], "ghost_events_compacted")
        self.assertEqual(control["created_at"], "2026-09-06T00:00:00Z")
        self.assertTrue(str(control["event_id"]).startswith("gec_"))


class OverCompactBudgetTests(unittest.TestCase):
    def test_at_budget_is_not_over(self) -> None:
        self.assertFalse(over_compact_budget({"events": 100, "bytes": 1000}, max_events=100, max_bytes=1000))

    def test_either_dimension_over_is_over(self) -> None:
        self.assertTrue(over_compact_budget({"events": 101, "bytes": 1000}, max_events=100, max_bytes=1000))
        self.assertTrue(over_compact_budget({"events": 100, "bytes": 1001}, max_events=100, max_bytes=1000))

    def test_garbage_stats_fail_closed(self) -> None:
        # No usage recorded compacts nothing; unparseable counters compact
        # defensively rather than trusting a broken reading.
        self.assertFalse(over_compact_budget({}, max_events=100, max_bytes=1000))
        self.assertTrue(over_compact_budget({"events": "many", "bytes": None}, max_events=100, max_bytes=1000))
