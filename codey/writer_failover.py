"""Writer failover state machine, isolated from Writer/Review business logic.

``TaskRunner.run`` used to keep the Writer takeover logic in a cluster of
closures that shared a lot of ``nonlocal`` state (current provider, switch
count, checkpoint view, turn budget). That made the state machine hard to reason
about and prove correct as provider resilience grows.

``WriterFailoverRunner`` owns exactly that state machine and nothing else:

* run one Writer attempt (delegated back to the caller),
* on ``ProviderActionError`` mark the provider failed and close it,
* pick the next healthy provider, run a canary when required,
* refresh the checkpoint view so the next Writer sees local facts,
* share the turn budget across attempts (a dropped provider has no full
  ``RunResult``, so the latest observed turn is used),
* honour Stop first, switch at most twice, and force a strict fresh chat after a
  switch.

It deliberately knows nothing about prompts, verification, project facts,
review, diffs, receipts or UI events. The caller supplies those through the
``attempt`` callback and a handful of small provider/checkpoint hooks, which
keeps the module unit-testable with plain fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

from codey.cancellation import TaskCancelled
from codey.provider_diagnostics import ProviderActionError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from codey.agent import RunResult
    from codey.provider_diagnostics import ProviderFailure
    from codey.providers import ChatProvider
    from codey.verification_policy import VerificationCandidate


@dataclass(frozen=True)
class CheckpointView:
    """Local facts a freshly activated Writer should see after a takeover."""

    prompt: str = ""
    changed_files: tuple[str, ...] = ()
    successful_checks: tuple[VerificationCandidate, ...] = ()


@dataclass(frozen=True)
class WriterAttempt:
    """Everything the failover runner controls for a single Writer attempt.

    The caller's ``attempt`` callback combines this with the Writer business
    inputs (project map, verified facts, verification loader, ...) that live in
    ``TaskRunner`` and are none of the runner's concern.
    """

    task: str
    provider_id: str
    provider: ChatProvider
    remaining_turns: int
    fresh_chat: bool
    strict_fresh_chat: bool
    handoff: str
    checkpoint: CheckpointView


class _TurnCounter:
    """Track the latest turn observed within one attempt.

    A provider that drops mid-attempt never returns a ``RunResult``, so the
    budget accounting relies on the highest turn seen through ``record``.
    """

    def __init__(self) -> None:
        self.turns = 0

    def record(self, turn: int) -> None:
        self.turns = max(self.turns, turn)


@dataclass
class WriterFailoverRunner:
    """Stateful Writer failover coordinator shared across Writer + Review repair.

    The instance is created once per task and reused for the initial Writer and
    any Review repair attempt, so the switch budget and tried-provider set are
    shared: a repair after one initial switch only gets one more switch, which
    matches the original closure behaviour.
    """

    provider: ChatProvider | None
    provider_id: str
    switches: int
    tried: set[str]
    attempt: Callable[[WriterAttempt, Callable[[int], None]], RunResult]
    select_next: Callable[[set[str]], str | None]
    connect: Callable[[str], ChatProvider]
    close: Callable[[ChatProvider], None]
    needs_canary: Callable[[str], bool]
    run_canary: Callable[[str, ChatProvider], bool]
    capture_failure: Callable[[str, str, BaseException], ProviderFailure]
    record_failure: Callable[[str, ProviderFailure], None]
    record_success: Callable[[str], None]
    clear_session: Callable[[str], None]
    on_switch: Callable[[str], None]
    refresh_checkpoint: Callable[[], CheckpointView]
    stopped: Callable[[], bool]
    max_switches: int = 2

    def run(
        self,
        *,
        task: str,
        turn_budget: int,
        fresh: bool,
        handoff: str,
        checkpoint: CheckpointView,
    ) -> RunResult:
        """Run the Writer with failover and return its (budget-adjusted) result."""
        turns_used = 0
        cur_fresh = fresh
        cur_handoff = handoff
        cur_checkpoint = checkpoint
        if self.provider is None:
            cur_fresh, cur_checkpoint = self._initial_reconnect(cur_fresh, cur_checkpoint)
        while True:
            counter = _TurnCounter()
            spec = WriterAttempt(
                task=task,
                provider_id=self.provider_id,
                provider=self.provider,
                remaining_turns=max(1, turn_budget - turns_used),
                fresh_chat=cur_fresh,
                strict_fresh_chat=self.switches > 0,
                handoff=cur_handoff,
                checkpoint=cur_checkpoint,
            )
            try:
                writer_result = self.attempt(spec, counter.record)
            except ProviderActionError as exc:
                if self.stopped():
                    raise TaskCancelled("task stopped") from exc
                turns_used += max(1, counter.turns)
                self.record_failure(self.provider_id, exc.failure)
                self.clear_session(self.provider_id)
                self._close_current()
                if self.switches >= self.max_switches or turns_used >= turn_budget:
                    raise
                cur_checkpoint = self._activate_next(exc)
                cur_fresh = True
                cur_handoff = ""
                continue
            self.record_success(self.provider_id)
            return replace(
                writer_result,
                turns=min(turn_budget, turns_used + writer_result.turns),
            )

    def _initial_reconnect(
        self,
        cur_fresh: bool,
        cur_checkpoint: CheckpointView,
    ) -> tuple[bool, CheckpointView]:
        """Reconnect a provider that was closed between calls (e.g. for Review).

        A fresh connect failure escalates through the same switch path as a
        mid-attempt failure. Unlike a mid-attempt failure it keeps the incoming
        handoff, matching the original behaviour.
        """
        try:
            self.provider = self.connect(self.provider_id)
        except TaskCancelled:
            raise
        except Exception as connect_error:
            failure = self.capture_failure(self.provider_id, "connect", connect_error)
            self.record_failure(self.provider_id, failure)
            self.clear_session(self.provider_id)
            error = ProviderActionError(failure)
            if self.switches >= self.max_switches:
                raise error from connect_error
            cur_checkpoint = self._activate_next(error)
            cur_fresh = True
        return cur_fresh, cur_checkpoint

    def _activate_next(self, origin: ProviderActionError) -> CheckpointView:
        """Select, connect and canary the next healthy provider.

        Returns the refreshed checkpoint view for the new Writer. Raises
        ``origin`` (or a connect ``ProviderActionError``) when no healthy
        provider remains within the switch budget.
        """
        while True:
            next_id = self.select_next(self.tried)
            if next_id is None:
                raise origin
            self.provider_id = next_id
            self.tried.add(next_id)
            self.switches += 1
            self.on_switch(next_id)
            try:
                self.provider = self.connect(next_id)
            except TaskCancelled:
                raise
            except Exception as connect_error:
                failure = self.capture_failure(next_id, "connect", connect_error)
                self.record_failure(next_id, failure)
                if self.switches >= self.max_switches:
                    raise ProviderActionError(failure) from connect_error
                continue
            if self.needs_canary(next_id) and not self.run_canary(next_id, self.provider):
                self._close_current()
                if self.switches >= self.max_switches:
                    raise origin
                continue
            return self.refresh_checkpoint()

    def _close_current(self) -> None:
        provider, self.provider = self.provider, None
        if provider is not None:
            try:
                self.close(provider)
            except Exception:
                pass


# Public surface intentionally small: the state machine plus its data shapes.
__all__ = [
    "CheckpointView",
    "WriterAttempt",
    "WriterFailoverRunner",
]
