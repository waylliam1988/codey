"""Capture the production SSE stream with an explicitly attached owner."""

from __future__ import annotations

import io
import socket
import threading
from collections.abc import Iterable, Sequence
from http.client import HTTPMessage

import pytest

from codey.app.context import AppContext
from codey.app.public_event_feed import EncodedEvent, FeedOverflow, PublicEventFeed, Subscription
from codey.app.server import CodeyHTTPServer, Handler
from codey.durable.session import DurableSession
from codey.utils.object_fields import decoded_object


class _CapturedContext(AppContext):
    def __init__(self, durable: DurableSession, replayed: Sequence[dict[str, object]]) -> None:
        self.durable = durable
        self._captured_replay = replayed

    def run_status(self) -> str:
        return "idle"

    def replay_events_after(
        self, last_event_id: int, *, max_event_id: int | None = None,
    ) -> list[tuple[int, dict[str, object]]]:
        cutoff = len(self._captured_replay) if max_event_id is None else max_event_id
        return [(index, event) for index, event in enumerate(self._captured_replay, 1)
                if last_event_id < index <= cutoff]


class _CapturedHandler(Handler):
    def __init__(self, ctx: AppContext, feed: PublicEventFeed, path: str, output: io.BytesIO) -> None:
        # Exercise Handler._sse and its real owner check, with an in-memory
        # output and an unbound socket used only for timeout state.
        owner = object.__new__(CodeyHTTPServer)
        owner.app_context = ctx
        owner.public_feed = feed
        owner.event_slots = threading.BoundedSemaphore(1)
        self.server = owner
        self.path = path
        self.headers = HTTPMessage()
        self.connection = socket.socket()
        self.wfile = output

    def send_response(self, code: int, message: str | None = None) -> None:
        return None

    def send_header(self, keyword: str, value: str) -> None:
        return None

    def end_headers(self) -> None:
        return None


def capture_events(
    durable: DurableSession, path: str, *, live: Iterable[dict[str, object]] = (),
    replayed: Sequence[dict[str, object]] = (),
) -> list[dict[str, object]]:
    feed = PublicEventFeed()
    feed.sequence = len(replayed)
    subscribe = feed.subscribe
    subscriptions: list[Subscription] = []
    ctx = _CapturedContext(durable, replayed)
    with io.BytesIO() as output, pytest.MonkeyPatch.context() as patch:
        def finite_subscribe() -> Subscription:
            item = subscribe()
            subscriptions.append(item)
            for index, event in enumerate(live, 1):
                item.offer(feed.encode(event, index))
            get = item.get

            def finite_get(timeout: float = 15) -> EncodedEvent:
                if not item.items:
                    raise FeedOverflow("test connection closed after available events")
                return get(timeout)

            patch.setattr(item, "get", finite_get)
            return item

        patch.setattr(feed, "subscribe", finite_subscribe)
        handler = _CapturedHandler(ctx, feed, path, output)
        try:
            Handler._sse(handler)
        finally:
            handler.connection.close()
        events = [decoded_object(line[6:].decode("utf-8")) for line in output.getvalue().split(b"\n")
                  if line.startswith(b"data: ")]
    assert not feed.subscribers
    assert all(item.closed for item in subscriptions)
    return events
