"""Explicit HTTP ownership fixtures for routes and direct worker tests."""

from codey.app import server, task_submit


def attach_public_events(httpd):
    """Attach the same public event owner used by LocalService to a raw test server."""
    from codey.app.public_event_feed import PublicEventFeed

    feed = PublicEventFeed()
    feed.sequence = httpd.app_context.event_bus.sequence
    detach = httpd.app_context.event_bus.observe(feed.publish)
    httpd.public_feed = feed

    def close():
        detach()
        feed.close()

    return close


def get_context():
    context = server.CodeyHTTPServer.app_context
    if context is None:
        raise AssertionError("test must attach its AppContext")
    return context


def submit(*args, **kwargs):
    return task_submit.submit_task(*args, **kwargs, get_state=get_context)


def continue_after_slot(*args, **kwargs):
    return task_submit.submit_task_after_slot_release(*args, **kwargs, get_state=get_context)
