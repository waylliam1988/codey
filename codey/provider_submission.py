"""One-shot boundary for remote provider message submission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from codey import cancellation, provider_controls


class SubmissionUncertain(TimeoutError):
    """The remote action ran, but the page did not confirm submission."""


@dataclass
class SendAttempt:
    phase: str = "ready"
    method: str = ""
    action_error: Exception | None = field(default=None, repr=False)

    def submit(self, method: str, action: Callable[[], None]) -> None:
        """Run exactly one remote submit action, marking it first."""
        if self.phase != "ready":
            raise RuntimeError("message submission was already attempted")
        cancellation.check()
        self.method = method
        self.phase = "attempted"
        try:
            action()
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            self.action_error = exc

    def confirm(self, on_click: Callable[[], None] | None = None) -> None:
        if self.phase == "confirmed":
            return
        if self.phase != "attempted":
            raise RuntimeError("message submission was not attempted")
        self.phase = "confirmed"
        if self.method == "click" and on_click is not None:
            on_click()

    @property
    def confirmed(self) -> bool:
        return self.phase == "confirmed"


def confirm_submission(attempt: SendAttempt, provider_id: str) -> None:
    """Confirm the attempt and remember a verified click control once."""
    attempt.confirm(lambda: provider_controls.confirm_control(
        provider_id, provider_controls.CONTROL_SEND_BUTTON
    ))
