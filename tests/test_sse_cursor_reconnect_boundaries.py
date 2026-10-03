"""Reconnect cursors neither invent gaps nor pin the stream after restart."""
from codey.app.event_bus import EventBus


def test_cursor_immediately_before_retained_window_replays_without_resync():
    bus = EventBus(replay_limit=2)
    for n in range(3):
        bus.emit({"type": "info", "n": n})
    assert bus.replay_events_after(1) == [(2, {"type": "info", "n": 1}), (3, {"type": "info", "n": 2})]


def test_future_cursor_after_restart_resets_to_current_cutoff():
    bus = EventBus(replay_limit=2)
    rows = bus.replay_events_after(500)
    assert rows == [(0, {"type": "resync_required", "reason": "sse_cursor_ahead", "cursor": 0})]
    bus.emit({"type": "info"})
    assert bus.replay_events_after(rows[0][1]["cursor"]) == [(1, {"type": "info"})]


def test_gap_marker_never_skips_live_event_after_subscription():
    bus = EventBus(replay_limit=1)
    for _ in range(5):
        bus.emit({"type": "info"})
    sub = bus.subscribe()
    bus.emit({"type": "tool"})
    rows = bus.replay_events_after(1, max_event_id=sub.replay_cutoff)
    assert rows[0][0] == rows[0][1]["cursor"] == 5
    assert sub.get_nowait().event_id == 6
    bus.unsubscribe(sub)


def test_expired_cursor_counts_only_unavailable_events():
    bus = EventBus(replay_limit=2)
    for _ in range(5):
        bus.emit({"type": "info"})
    marker = bus.replay_events_after(1)[0][1]
    # IDs 2 and 3 expired; 4 and 5 are retained.
    assert marker["dropped"] == 2
