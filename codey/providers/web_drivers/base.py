"""Shared response-stability loop for browser-backed provider drivers."""

from __future__ import annotations

import time
from typing import Callable

from codey.providers import controls
from codey.providers import flow as provider_flow
from codey.providers import send_loop
from codey.providers.diagnostics import ResponseMissing
from codey.providers.submission import SendAttempt, confirm_submission
from codey.providers.submission import SubmissionUncertain
from codey.runtime import cancellation


def wait_for_stable_completion(
    ctx: send_loop.ProviderSendContext,
    attempt: SendAttempt,
    *,
    response_timeout: float,
    stable_ticks: int,
    tick: float,
    min_wait: float,
    read_current: Callable[[], str],
    read_final: Callable[[], str],
    read_late: Callable[[], str],
    uncertain_message: str,
    before_poll: Callable[[send_loop.ProviderSendContext], bool | None] | None = None,
    before_return: Callable[[], None] | None = None,
    is_json_tool: Callable[[str], bool] | None = None,
    looks_like_json_tool: Callable[[str], bool] | None = None,
    repair_json_tool: Callable[[str], str] | None = None,
    json_tool_stable_ticks: int = 2,
    built_in_ready: Callable[[int, provider_flow.FlowObservation], bool] | None = None,
    allow_recovery: bool = False,
    before_recover: Callable[[], None] | None = None,
    missing_message: str | None = None,
) -> str:
    """Wait for a provider response to stabilize, then read final text."""

    def finish(reader: Callable[[], str]) -> str:
        if before_return is not None:
            before_return()
        return send_loop.read_completion(ctx, reader)

    overall_deadline = time.time() + max(0.0, response_timeout)
    while time.time() < overall_deadline:
        cancellation.wait(tick)
        if before_poll is not None:
            if before_poll(ctx):
                continue
        current = read_current()
        if not current:
            continue
        confirm_submission(attempt, ctx.provider_id)
        ctx.appeared = True
        same = ctx.same_as_last(current)
        observation = provider_flow.FlowObservation(
            response_stable=same,
            response_nonempty=True,
        )
        ctx.record_response(current, observation)
        if not same or (time.time() - ctx.sent_at) < min_wait:
            continue

        is_json = bool(is_json_tool and is_json_tool(current))
        repairable_json = False
        if looks_like_json_tool and looks_like_json_tool(current) and not is_json:
            repairable_json = bool(repair_json_tool and repair_json_tool(current))
            if not repairable_json and ctx.stable < stable_ticks:
                continue
        if ctx.stable >= json_tool_stable_ticks and is_json:
            return finish(read_final)
        if repairable_json and ctx.stable < stable_ticks:
            continue
        if repairable_json:
            return finish(read_final)

        ready = send_loop.completion_ready(
            ctx,
            observation,
            built_in_ready=(
                built_in_ready(ctx.stable, observation)
                if built_in_ready is not None
                else ctx.stable >= stable_ticks
            ),
            allow_recovery=allow_recovery,
        )
        if ready:
            return finish(read_final)

    if before_recover is not None:
        before_recover()
    late = read_late()
    if late:
        confirm_submission(attempt, ctx.provider_id)
        if before_return is not None:
            before_return()
        return late

    if ctx.appeared and ctx.last:
        if before_return is not None:
            before_return()
        return read_final()

    recovered = controls.recover_response(ctx.page, ctx.provider_id, read_final)
    if recovered is not None:
        confirm_submission(attempt, ctx.provider_id)
        if before_return is not None:
            before_return()
        return recovered

    if not attempt.confirmed:
        if attempt.method == "click" and attempt.action_error is not None:
            controls.reject_control(ctx.provider_id, controls.CONTROL_SEND_BUTTON)
        raise SubmissionUncertain(uncertain_message)

    raise ResponseMissing(
        missing_message
        or f"{ctx.display_name} response timed out after {response_timeout:.0f}s"
    )


__all__ = ["wait_for_stable_completion"]
