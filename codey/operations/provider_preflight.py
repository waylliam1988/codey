from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from codey.policies.action import DECISION_DENY, ActionSubject, evaluate_action
from codey.providers import PROVIDER_LABELS
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.providers.supervisor import run_half_open_canary
from codey.runs.ledger import RunLedgerWriter
from codey.runtime.cancellation import TaskCancelled


@dataclass(frozen=True)
class ProviderPreflightResult:
    provider: Any
    provider_id: str
    tried: set[str]
    switches: int


class ProviderSwitchDenied(RuntimeError):
    """Raised when policy denies a provider fallback before state mutation."""


def provider_fallback_policy_decision(
    *,
    from_provider: str,
    to_provider: str,
    phase: str,
):
    return evaluate_action(ActionSubject(
        kind="provider_fallback",
        phase=phase,
        from_provider=from_provider,
        to_provider=to_provider,
    ))


def connect_provider_with_preflight(
    *,
    state: Any,
    run_id: str,
    provider_id: str,
    supervisor: Any | None,
    ranked_failover_order: Callable[[], tuple[str, ...]],
    capture_provider_failure: Callable[..., ProviderFailure],
    record_provider_failure: Callable[[str, ProviderFailure], None],
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    trace_sink: Any,
) -> ProviderPreflightResult:
    preflight_tried: set[str] = set()
    preflight_switches = 0
    if supervisor is not None:
        supervisor.prepare_user_selected(provider_id)
    if supervisor is not None and not supervisor.is_available(provider_id):
        replacement_id = supervisor.select(
            "",
            ranked_failover_order(),
            excluded=(provider_id,),
        )
        if replacement_id is None:
            raise RuntimeError("selected provider is unavailable")
        previous_provider_id = provider_id
        provider_id = replacement_id
        ensure_provider_fallback_allowed(
            trace_sink,
            from_provider=previous_provider_id,
            to_provider=provider_id,
            phase="preflight",
        )
        state.switch_run_provider(run_id, provider_id)
        _record_switch(
            append_ledger,
            trace_sink,
            from_provider=previous_provider_id,
            to_provider=provider_id,
            phase="preflight",
            reason="unavailable",
        )
        preflight_switches = 1

    while True:
        preflight_tried.add(provider_id)
        try:
            provider = state.get_provider(provider_id)
        except TaskCancelled:
            raise
        except ProviderActionError:
            raise
        except Exception as connect_error:
            failure = capture_provider_failure(
                model=PROVIDER_LABELS.get(provider_id, provider_id),
                action="connect",
                page=None,
                error=connect_error,
            )
            record_provider_failure(provider_id, failure)
            if preflight_switches >= 2:
                raise ProviderActionError(failure) from connect_error
            if supervisor is None:
                raise ProviderActionError(failure) from connect_error
            replacement_id = supervisor.select(
                "",
                ranked_failover_order(),
                excluded=preflight_tried,
            )
            if replacement_id is None:
                raise ProviderActionError(failure) from connect_error
            previous_provider_id = provider_id
            provider_id = replacement_id
            ensure_provider_fallback_allowed(
                trace_sink,
                from_provider=previous_provider_id,
                to_provider=provider_id,
                phase="connect",
            )
            preflight_switches += 1
            state.switch_run_provider(run_id, provider_id)
            _record_switch(
                append_ledger,
                trace_sink,
                from_provider=previous_provider_id,
                to_provider=provider_id,
                phase="connect",
                reason="provider_failure",
            )
            continue
        if (
            supervisor is None
            or not supervisor.needs_canary(provider_id)
            or run_half_open_canary(provider_id, provider, supervisor)
        ):
            return ProviderPreflightResult(
                provider=provider,
                provider_id=provider_id,
                tried=preflight_tried,
                switches=preflight_switches,
            )
        try:
            provider.close()
        except Exception as close_error:
            failure = capture_provider_failure(
                model=PROVIDER_LABELS.get(provider_id, provider_id),
                action="close",
                page=None,
                error=close_error,
            )
            record_provider_failure(provider_id, failure)
        if preflight_switches >= 2:
            raise RuntimeError("no healthy provider available after canary failure")
        replacement_id = supervisor.select(
            "",
            ranked_failover_order(),
            excluded=preflight_tried,
        )
        if replacement_id is None:
            raise RuntimeError("no healthy provider available after canary failure")
        previous_provider_id = provider_id
        provider_id = replacement_id
        ensure_provider_fallback_allowed(
            trace_sink,
            from_provider=previous_provider_id,
            to_provider=provider_id,
            phase="canary",
        )
        preflight_switches += 1
        state.switch_run_provider(run_id, provider_id)
        _record_switch(
            append_ledger,
            trace_sink,
            from_provider=previous_provider_id,
            to_provider=provider_id,
            phase="canary",
            reason="provider_failure",
        )


def ensure_provider_fallback_allowed(
    trace_sink: Any,
    *,
    from_provider: str,
    to_provider: str,
    phase: str,
):
    decision = provider_fallback_policy_decision(
        from_provider=from_provider,
        to_provider=to_provider,
        phase=phase,
    )
    trace_sink.call("record_policy_decision", decision)
    if decision.decision == DECISION_DENY:
        raise ProviderSwitchDenied(decision.reason_code)
    return decision


def _record_switch(
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    trace_sink: Any,
    *,
    from_provider: str,
    to_provider: str,
    phase: str,
    reason: str,
) -> None:
    append_ledger(
        lambda ledger: ledger.append(
            "provider_switched",
            from_provider=from_provider,
            to_provider=to_provider,
            phase=phase,
            reason=reason,
        )
    )
    trace_sink.call(
        "record_fallback",
        from_provider=from_provider,
        to_provider=to_provider,
        phase=phase,
        reason_code=reason,
    )


__all__ = [
    "ProviderPreflightResult",
    "ProviderSwitchDenied",
    "connect_provider_with_preflight",
    "ensure_provider_fallback_allowed",
    "provider_fallback_policy_decision",
]
