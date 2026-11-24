"""Observer reentry must terminate and preserve event cursor ordering."""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

import pytest

from codey.app.event_bus import EventBus, EventSubscriber
from codey.utils.object_fields import decoded_object


def probe(operation: str) -> int:
    """Isolate a deadlocking old observer in a short-lived process."""
    bus = EventBus(replay_limit=8)
    queued = bus.subscribe()
    entered = Event()
    seen: list[str] = []
    sequences: list[int] = []
    attached: list[EventSubscriber] = []
    errors: list[str] = []
    unsubscribe: Callable[[], None]

    def first(sequence: int, event: dict[str, object]) -> None:
        entered.set()
        seen.append(f"A{sequence}")
        if operation == "sequence":
            sequences.append(bus.sequence)
        elif operation == "unsubscribe":
            unsubscribe()
        elif operation == "subscribe" and not attached:
            attached.append(bus.subscribe())
        elif operation == "emit" and sequence == 1:
            bus.emit({"type": "info", "text": "nested"})

    def second(sequence: int, event: dict[str, object]) -> None:
        seen.append(f"B{sequence}")

    unsubscribe = bus.observe(first)
    bus.observe(second)

    def publish() -> None:
        try:
            bus.emit({"type": "info", "text": "outer"})
        except Exception as exc:
            errors.append(repr(exc))

    worker = Thread(target=publish, daemon=True)
    worker.start()
    worker.join(1)
    finished = not worker.is_alive()
    if finished and operation in {"unsubscribe", "subscribe"}:
        bus.emit({"type": "info", "text": "later"})
    report: dict[str, object] = {
        "entered": entered.is_set(), "finished": finished, "errors": errors,
        "seen": seen, "sequences": sequences,
        "queued": [queued.get_nowait().event_id for _ in range(queued.qsize())],
        "attached": [[item.get_nowait().event_id for _ in range(item.qsize())] for item in attached],
    }
    print(json.dumps(report))
    return 0 if finished and not errors else 1


@pytest.mark.parametrize("operation", ["sequence", "unsubscribe", "subscribe", "emit"])
def test_observer_reentry_preserves_publication_order(operation: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from tests.test_event_bus_observers import probe; import sys; "
         "raise SystemExit(probe(sys.argv[1]))", operation],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, f"observer reentry did not finish: {result.stdout} {result.stderr}"
    report = decoded_object(result.stdout)
    assert report["entered"] is True and report["finished"] is True and report["errors"] == []
    if operation == "sequence":
        assert report["sequences"] == [1]
        assert report["seen"] == ["A1", "B1"]
        assert report["queued"] == [1]
    elif operation == "unsubscribe":
        assert report["seen"] == ["A1", "B1", "B2"]
        assert report["queued"] == [1, 2]
    elif operation == "subscribe":
        assert report["seen"] == ["A1", "B1", "A2", "B2"]
        assert report["attached"] == [[2]]
    else:
        assert report["seen"] == ["A1", "B1", "A2", "B2"]
        assert report["queued"] == [1, 2]


def test_other_emitters_wait_for_ordered_observer_delivery() -> None:
    bus = EventBus(replay_limit=8)
    entered, release, second_finished = Event(), Event(), Event()
    seen: list[int] = []

    def observe(sequence: int, event: dict[str, object]) -> None:
        seen.append(sequence)
        if sequence == 1:
            entered.set()
            assert release.wait(5)

    bus.observe(observe)

    def second() -> None:
        bus.emit({"type": "info", "n": 2})
        second_finished.set()

    first_event: dict[str, object] = {"type": "info", "n": 1}
    first = Thread(target=bus.emit, args=(first_event,))
    later = Thread(target=second)
    first.start()
    try:
        assert entered.wait(5)
        later.start()
        assert not second_finished.wait(0.05)
    finally:
        release.set()
        first.join(5)
        if later.ident is not None:
            later.join(5)
    assert not first.is_alive() and not later.is_alive()
    assert second_finished.is_set() and seen == [1, 2]


def test_observer_error_after_reentry_keeps_delivery_and_bus_usable(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus(replay_limit=8)
    subscriber = bus.subscribe()
    seen: list[str] = []

    def first(sequence: int, event: dict[str, object]) -> None:
        seen.append(f"A{sequence}")
        if sequence == 1:
            bus.emit({"type": "info", "text": "nested"})
            raise ValueError("observer fixture failed")

    def second(sequence: int, event: dict[str, object]) -> None:
        seen.append(f"B{sequence}")

    bus.observe(first)
    bus.observe(second)
    bus.emit({"type": "info", "text": "outer"})
    bus.emit({"type": "info", "text": "later"})
    assert seen == ["A1", "B1", "A2", "B2", "A3", "B3"]
    assert [subscriber.get_nowait().event_id for _ in range(subscriber.qsize())] == [1, 2, 3]
    assert "event observer failed" in caplog.text and "observer fixture failed" in caplog.text
