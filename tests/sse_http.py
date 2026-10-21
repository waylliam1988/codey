"""Capture the production SSE stream with an explicitly attached owner."""

import io
import json
from types import SimpleNamespace

from codey.app.public_event_feed import FeedOverflow, PublicEventFeed
from codey.app.server import Handler


def capture_events(durable, path, *, live=(), replayed=()):
    feed = PublicEventFeed()
    feed.sequence = len(replayed)
    subscribe = feed.subscribe
    subscriptions = []

    def finite_subscribe():
        item = subscribe()
        subscriptions.append(item)
        for index, event in enumerate(live, 1):
            item.offer(feed.encode(event, index))
        get = item.get

        def finite_get():
            if not item.items:
                raise FeedOverflow("test connection closed after available events")
            return get()

        item.get = finite_get
        return item

    feed.subscribe = finite_subscribe
    def replay_events_after(cursor, *, max_event_id):
        return [(index, event) for index, event in enumerate(replayed, 1) if cursor < index <= max_event_id]

    ctx = SimpleNamespace(durable=durable, run_status=lambda: {}, replay_events_after=replay_events_after)
    output = io.BytesIO()
    handler = SimpleNamespace(path=path, headers={}, connection=SimpleNamespace(gettimeout=lambda: None, settimeout=lambda _: None),
        wfile=output, server=SimpleNamespace(app_context=ctx, public_feed=feed), _get_state=lambda: ctx,
        send_response=lambda *_: None, send_header=lambda *_: None, end_headers=lambda: None)
    Handler._sse(handler)
    events = [json.loads(line[6:]) for line in output.getvalue().split(b"\n") if line.startswith(b"data: ")]
    assert not feed.subscribers
    assert all(item.closed for item in subscriptions)
    return events
