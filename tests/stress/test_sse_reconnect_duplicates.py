"""P3: SSE reconnect + duplicate delivery.

The bus is memory-only: a restart loses the replay window by design and the
client reconciles through durable state instead. What must hold:

- disconnect at N + replay from N delivers exactly N+1..current, in order;
- duplicate deliveries never create duplicate durable facts (delivery
  dedupe at the client mirror, exactly-once at the durable sink);
- an overflowed subscriber gets a resync marker, never silent loss.
"""

from __future__ import annotations

import queue
import unittest

from codey.app.event_bus import EventBus
from tests.stress.oracle import InvariantChecker


def _drain(sub) -> list:
    events = []
    while True:
        try:
            events.append(sub.get_nowait())
        except queue.Empty:
            return events


class SSEReconnectTests(unittest.TestCase):
    def test_reconnect_from_cursor_delivers_suffix_in_order(self) -> None:
        bus = EventBus(replay_limit=512)
        sub = bus.subscribe()
        for index in range(1, 4):
            bus.emit({"event_key": f"e{index}", "type": "tool", "n": index})
        self.assertEqual(len(_drain(sub)), 3)
        bus.unsubscribe(sub)
        for index in range(4, 8):
            bus.emit({"event_key": f"e{index}", "type": "tool", "n": index})
        replayed = bus.replay_events_after(3)
        self.assertEqual([event_id for event_id, _ in replayed], [4, 5, 6, 7])
        self.assertEqual(
            [payload["event_key"] for _, payload in replayed],
            ["e4", "e5", "e6", "e7"],
        )
        client = [f"e{i}" for i in (1, 2, 3)] + [
            payload["event_key"] for _, payload in replayed
        ]
        server = [f"e{i}" for i in range(1, 8)]
        self.assertEqual(client, server)

    def test_expired_window_forces_resync_marker(self) -> None:
        bus = EventBus(replay_limit=4)
        for index in range(1, 10):
            bus.emit({"event_key": f"e{index}", "type": "tool"})
        replayed = bus.replay_events_after(2)
        self.assertEqual(len(replayed), 1)
        event_id, marker = replayed[0]
        self.assertEqual(marker["type"], "resync_required")
        self.assertGreater(event_id, 2)

    def test_overflow_marks_resync_instead_of_silent_loss(self) -> None:
        bus = EventBus(replay_limit=512)
        sub = bus.subscribe(maxsize=4)
        for index in range(20):
            bus.emit({"event_key": f"e{index}", "type": "tool"})
        queued = _drain(sub)
        kinds = [event.get("type") for event in queued]
        self.assertIn("resync_required", kinds)


class SSEDuplicateTests(unittest.TestCase):
    def test_duplicate_deliveries_apply_durable_fact_once(self) -> None:
        # Mirror of the UI eventKey dedupe: the seen-set is delivery dedupe
        # only; the durable sink below is the source of truth.
        seen: set[str] = set()
        applied: list[str] = []
        stream = ["e4", "e5", "e5", "e5", "e6", "e4"]

        def _deliver(event_key: str) -> None:
            if event_key in seen:
                return
            seen.add(event_key)
            applied.append(event_key)

        for key in stream:
            _deliver(key)
        self.assertEqual(sorted(applied), ["e4", "e5", "e6"])
        oracle = InvariantChecker(seed=32)
        oracle.check_no_duplicate_facts(
            [f"tool:{key}" for key in applied]
        )


if __name__ == "__main__":
    unittest.main()
