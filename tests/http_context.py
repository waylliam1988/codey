"""Explicit HTTP ownership fixtures for routes and direct worker tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Unpack

from codey.app import server, task_submit
from codey.app.submission_types import ContinuationOptions, TaskSubmitOptions

if TYPE_CHECKING:
    from codey.app.context import AppContext


class _SubmitOptions(TaskSubmitOptions, total=False):
    abort_if_stopped: bool


class _ContinuationOptions(ContinuationOptions, total=False):
    timeout: float


def attach_public_events(httpd: server.CodeyHTTPServer) -> Callable[[], None]:
    """Attach the same public event owner used by LocalService to a raw test server."""
    from codey.app.public_event_feed import PublicEventFeed

    context = httpd.app_context
    if context is None:
        raise AssertionError("test must attach its AppContext")
    feed = PublicEventFeed()
    feed.sequence = context.event_bus.sequence
    publish: Callable[[int, dict[str, object]], None] = feed.publish
    detach = context.event_bus.observe(publish)
    httpd.public_feed = feed

    def close() -> None:
        detach()
        feed.close()

    return close


def get_context() -> AppContext:
    context = server.CodeyHTTPServer.app_context
    if context is None:
        raise AssertionError("test must attach its AppContext")
    return context


def submit(
    session_id: str, project: str | None, task: str, max_turns: int,
    continue_task: bool, provider_id: str, intent: str = "auto", **kwargs: Unpack[_SubmitOptions],
) -> str | None:
    return task_submit.submit_task(
        session_id, project, task, max_turns, continue_task, provider_id, intent,
        **kwargs, get_state=get_context,
    )


def continue_after_slot(
    session_id: str, project: str | None, task: str, max_turns: int,
    continue_task: bool, provider_id: str, intent: str = "auto", **kwargs: Unpack[_ContinuationOptions],
) -> str | None:
    return task_submit.submit_task_after_slot_release(
        session_id, project, task, max_turns, continue_task, provider_id, intent,
        **kwargs, get_state=get_context,
    )
