"""Thin shared send/new_chat plumbing for the web provider wrappers.

The five ``providers/*_web.py`` wrappers previously each re-implemented the
same deadline wiring, and the send deadline equalled the inner
``response_timeout`` -- even though every driver keeps polling through an
internal timeout grace before giving up. The outer scope therefore always
fired first and surfaced as a transient failure instead of the honest
"model never answered".

``run_web_send`` sizes the outer budget as
``response_timeout + grace + margin`` so the driver's own completion path
wins, and classifies a still-firing outer deadline as ``response_missing``.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from codey import cancellation
from codey.provider_diagnostics import (
    ProviderActionError,
    ResponseMissing,
    capture_provider_failure,
    run_provider_action,
)
from codey.provider_timeouts import start_deadline

WEB_DEADLINE_MARGIN_SECONDS = 5.0

T = TypeVar("T")


def run_web_new_chat(
    provider: Any,
    *,
    page: Any,
    func: Callable[[], T],
    timeout: float | None,
) -> T:
    with cancellation.deadline_scope(start_deadline(timeout)):
        return run_provider_action(
            provider,
            action="new_chat",
            page=page,
            func=func,
        )


def run_web_send(
    provider: Any,
    *,
    page: Any,
    func: Callable[[], T],
    response_timeout: float | None,
    grace: float,
) -> T:
    budget = None if response_timeout is None else max(0.0, float(response_timeout))
    if budget is not None:
        budget += max(0.0, float(grace)) + WEB_DEADLINE_MARGIN_SECONDS
    try:
        with cancellation.deadline_scope(start_deadline(budget)):
            return run_provider_action(
                provider,
                action="send",
                page=page,
                func=func,
            )
    except cancellation.DeadlineExceeded as exc:
        _raise_response_missing(provider, page=page, cause=exc)
    except ProviderActionError as exc:
        # run_provider_action wraps a driver-side DeadlineExceeded into a
        # generic failure before we ever see it; recover the true cause and
        # classify it honestly instead of letting it read as transient.
        if isinstance(exc.__cause__, cancellation.DeadlineExceeded):
            _raise_response_missing(provider, page=page, cause=exc.__cause__)
        raise


def _raise_response_missing(
    provider: Any,
    *,
    page: Any,
    cause: BaseException,
) -> None:
    # Route through the standard capture so the failure keeps the same
    # on-site diagnostics (url/title/time/stage/facts) as any other
    # provider failure; ResponseMissing already carries the
    # response_missing kind.
    error = ResponseMissing("provider response deadline exceeded")
    failure = capture_provider_failure(
        model=str(getattr(provider, "name", "") or ""),
        action="send",
        page=page,
        error=error,
    )
    provider.last_failure = failure
    raise ProviderActionError(failure) from cause


__all__ = [
    "WEB_DEADLINE_MARGIN_SECONDS",
    "run_web_new_chat",
    "run_web_send",
]
