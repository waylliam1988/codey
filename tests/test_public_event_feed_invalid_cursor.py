"""Invalid public cursors must produce the documented reconnect loss signal."""

from __future__ import annotations

import pytest

from codey.app.public_event_feed import FeedOverflow, PublicEventFeed


@pytest.mark.parametrize("cursor", ["not-an-integer", ["invalid"], {"invalid": 1}])
def test_invalid_commit_cursor_closes_subscribers_to_signal_loss(cursor: object) -> None:
    feed = PublicEventFeed()
    subscribers = [feed.subscribe(), feed.subscribe()]
    event: dict[str, object] = {"type": "durable_commit", "commit_seq": cursor}
    feed.publish(1, event)
    for subscriber in subscribers:
        with pytest.raises(FeedOverflow):
            subscriber.get(timeout=0)
    assert feed.encodes == 0
