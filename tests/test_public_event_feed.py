"""Public frame identity and cache bounds without a service snapshot."""

from __future__ import annotations

import pytest

from codey.app.public_event_feed import FeedOverflow, PrivateEvent, PublicEventFeed
from codey.utils.object_fields import decoded_object


def test_encoding_without_a_cursor_does_not_reuse_another_events_body() -> None:
    feed = PublicEventFeed()
    first = feed.encode({"type": "info", "session_id": "first", "text": "alpha"})
    second = feed.encode({"type": "info", "session_id": "second", "text": "beta"})

    first_body = decoded_object(first.body.decode())
    assert first_body["session_id"] == "first"
    assert first_body["text"] == "alpha"
    second_body = decoded_object(second.body.decode())
    assert second_body["session_id"] == "second"
    assert second_body["text"] == "beta"
    assert second.session_id == "second"


def test_uncursored_public_frame_does_not_hide_a_private_event() -> None:
    feed = PublicEventFeed()
    feed.encode({"type": "info", "text": "public"})

    with pytest.raises(PrivateEvent):
        feed.encode({"type": "private_diagnostic", "secret": "not public"})


def test_positive_event_and_commit_cursors_reuse_the_same_body() -> None:
    feed = PublicEventFeed()
    event: dict[str, object] = {"type": "info", "session_id": "chat", "text": "hello"}
    commit: dict[str, object] = {"type": "durable_commit", "session_id": "chat", "commit_seq": 7}
    first = feed.encode(event, 7)
    durable = feed.encode(commit)
    assert feed.encode(event, 7).body is first.body
    assert feed.encode(commit, 99).body is durable.body
    assert first.body != durable.body
    assert feed.encodes == 2
    assert feed.cache_size == len(first.body) + len(durable.body)


def test_uncursored_encoding_does_not_evict_frames_or_consume_cache_bytes() -> None:
    feed = PublicEventFeed(cache_bytes=256)
    original = feed.encode({"type": "info", "text": "original"}, 1)
    original_size = feed.cache_size
    for index in range(20):
        feed.encode({"type": "info", "text": "x" * index})
    assert feed.encodes == 21
    assert feed.cache_size == original_size
    assert feed.encode({"type": "info", "text": "original"}, 1).body is original.body
    assert feed.encodes == 21


@pytest.mark.parametrize("sequence", [0, -1])
def test_publishing_without_a_positive_cursor_does_not_alias_frames(sequence: int) -> None:
    feed = PublicEventFeed()
    subscriber = feed.subscribe()
    for text in ("alpha", "beta"):
        feed.publish(sequence, {"type": "info", "text": text})
        assert decoded_object(subscriber.get(timeout=0).body.decode())["text"] == text
    assert not feed.cache
    feed.close()


def test_uncursored_frames_still_enforce_the_frame_limit() -> None:
    feed = PublicEventFeed(max_frame_bytes=128)
    with pytest.raises(FeedOverflow, match="frame budget"):
        feed.encode({"type": "info", "text": "x" * 256})
    assert not feed.cache
    assert feed.encodes == 0


def test_cache_eviction_preserves_its_byte_and_entry_bounds() -> None:
    feed = PublicEventFeed(cache_bytes=128)
    for sequence in range(1, 10):
        feed.encode({"type": "info", "text": str(sequence)}, sequence)
        assert feed.cache_size <= feed.cache_limit
        assert feed.cache_size == sum(len(body) for body in feed.cache.values())
    assert ("event", 1) not in feed.cache

    many = PublicEventFeed()
    for sequence in range(1, 1002):
        many.encode({"type": "info", "text": str(sequence)}, sequence)
    assert len(many.cache) == 1000
    assert ("event", 1) not in many.cache
    many.close()
    assert not many.cache
    assert many.cache_size == 0
