"""Small shared send-loop helpers for web provider drivers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from codey.providers import controls as controls
from codey.providers import flow as provider_flow
from codey.providers.diagnostics import ResponseMissing
from codey.providers.submission import (
    SendAttempt,
    SubmissionUncertain,
    confirm_submission,
)


@contextmanager
def response_watch(page: Any, provider_id: str) -> Iterator[None]:
    controls.start_response_watch(page, provider_id)
    try:
        yield
    finally:
        controls.stop_response_watch(page, provider_id)


@dataclass
class ProviderSendContext:
    page: Any
    provider_id: str
    display_name: str
    sent_at: float
    appeared: bool = False
    last: str = ""
    stable: int = 0
    trace: provider_flow.FlowTrace = field(default_factory=provider_flow.FlowTrace)

    def same_as_last(self, current: str) -> bool:
        return bool(current) and current == self.last

    def record_response(
        self,
        current: str,
        observation: provider_flow.FlowObservation,
    ) -> None:
        self.appeared = True
        self.trace.add(observation)
        if not current:
            self.stable = 0
            return
        if current == self.last:
            self.stable += 1
        else:
            self.stable = 0
            self.last = current

    def reset_text_progress(self, *, sent_at: float | None = None) -> None:
        """Reset text stability while preserving flow trace history."""
        if sent_at is not None:
            self.sent_at = sent_at
        self.appeared = False
        self.last = ""
        self.stable = 0


def completion_ready(
    ctx: ProviderSendContext,
    observation: provider_flow.FlowObservation,
    *,
    built_in_ready: bool,
    allow_recovery: bool = False,
) -> bool:
    return controls.flow_stage_ready(
        ctx.page,
        ctx.provider_id,
        provider_flow.STAGE_COMPLETION,
        ctx.trace,
        observation,
        built_in_ready=built_in_ready,
        allow_recovery=allow_recovery,
    )


def read_completion(ctx: ProviderSendContext, reader: Callable[[], str]) -> str:
    return controls.read_flow_response(
        ctx.provider_id,
        provider_flow.STAGE_COMPLETION,
        reader,
    )


def recover_or_raise(
    ctx: ProviderSendContext,
    attempt: SendAttempt,
    *,
    read_final: Callable[[], str],
    read_late: Callable[[], str],
    response_timeout: float,
    uncertain_message: str,
    missing_message: str | None = None,
) -> str:
    late = read_late()
    if late:
        confirm_submission(attempt, ctx.provider_id)
        return late

    if ctx.appeared and ctx.last:
        return read_final()

    recovered = controls.recover_response(ctx.page, ctx.provider_id, read_final)
    if recovered is not None:
        confirm_submission(attempt, ctx.provider_id)
        return recovered

    if not attempt.confirmed:
        if attempt.method == "click" and attempt.action_error is not None:
            controls.reject_control(ctx.provider_id, controls.CONTROL_SEND_BUTTON)
        raise SubmissionUncertain(uncertain_message)

    raise ResponseMissing(
        missing_message
        or f"{ctx.display_name} response timed out after {response_timeout:.0f}s"
    )