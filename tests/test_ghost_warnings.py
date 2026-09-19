from __future__ import annotations

import unittest

from codey.ghost import affinity, continuity, directive, hebbian, inbox, router, sleep, work_queue
from codey.ghost._warnings import (
    bounded_warnings,
    event_read_warnings,
    map_event_warnings,
    slice_event_warnings,
)


class GhostWarningsEquivalenceTests(unittest.TestCase):
    def test_bounded_helpers_match_shared_loop(self) -> None:
        sample = ["b", "a", "b", "", "a", "c"]
        self.assertEqual(
            work_queue._bounded_warnings(sample),
            bounded_warnings(sample, limit=work_queue.MAX_WORK_WARNINGS, redact_sensitive=True),
        )
        self.assertEqual(
            affinity._bounded_warnings(sample),
            bounded_warnings(sample, limit=affinity.MAX_AFFINITY_WARNINGS),
        )
        self.assertEqual(
            continuity._bounded_warnings(sample),
            bounded_warnings(sample, limit=continuity.MAX_CONTINUITY_WARNINGS),
        )
        self.assertEqual(
            directive._bounded_warnings(list(sample)),
            bounded_warnings(sample, limit=directive.MAX_DIRECTIVE_WARNINGS),
        )
        self.assertEqual(
            sleep._bounded_warnings(sample),
            bounded_warnings(sample, limit=sleep.MAX_SLEEP_WARNINGS, redact_sensitive=True),
        )

    def test_event_mappers_match_shared_helpers(self) -> None:
        self.assertEqual(
            work_queue._event_read_warnings(["work_events.jsonl:too_large", "x"]),
            event_read_warnings(
                ["work_events.jsonl:too_large", "x"],
                stream="work_events",
                limit=work_queue.MAX_WORK_WARNINGS,
                redact_sensitive=True,
            ),
        )
        self.assertEqual(
            affinity._event_read_warnings(["affinity_events.jsonl:unreadable"]),
            ("affinity_events_unreadable",),
        )
        self.assertEqual(
            continuity._event_read_warnings(["continuity_events.jsonl:too_large"]),
            ("continuity_events_too_large",),
        )
        self.assertEqual(
            hebbian._event_read_warnings(["hebbian_events.jsonl:too_large", "x"]),
            slice_event_warnings(
                ["hebbian_events.jsonl:too_large", "x"],
                stream="hebbian_events",
                limit=hebbian.MAX_HEBBIAN_WARNINGS,
            ),
        )
        self.assertEqual(
            inbox._event_read_warnings(["events.jsonl:unreadable"]),
            ("events_unreadable",),
        )
        self.assertEqual(
            router._event_read_warnings(["router_events.jsonl:too_large", "x"]),
            tuple(map_event_warnings(["router_events.jsonl:too_large", "x"], stream="router_events")),
        )
        self.assertEqual(
            sleep._sleep_event_read_warnings(["sleep_events.jsonl:too_large"]),
            ("sleep_events_too_large",),
        )


if __name__ == "__main__":
    unittest.main()
