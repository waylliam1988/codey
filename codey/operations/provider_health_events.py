from __future__ import annotations

import logging
from collections.abc import Callable

from codey.providers.diagnostics import ProviderFailure
from codey.providers.supervisor import HealthStoreError, ProviderSupervisor
from codey.repairs.self_repair import SelfRepairSupervisor
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace

logger = logging.getLogger(__name__)


def record_provider_failure_event(
    append_ledger_provider_failure: Callable[[str, ProviderFailure], None],
    trace_sink: FailOpenPromptTrace,
    supervisor: ProviderSupervisor | None,
    self_repair: SelfRepairSupervisor | None,
    pid: str,
    failure: ProviderFailure,
) -> None:
    """Fan out one provider failure to ledger, trace, supervisor, self-repair."""
    append_ledger_provider_failure(pid, failure)
    trace_sink.call("record_provider_failure", pid, failure)
    if supervisor is None:
        return
    try:
        health = supervisor.record_failure(pid, failure)
    except HealthStoreError:
        # The failure is already in the ledger; without durable health there
        # is nothing honest to enqueue for repair, so log and continue with
        # provider cleanup/failover instead of crashing the run.
        logger.exception("provider health record failed for %s", pid)
        return
    if self_repair is not None:
        try:
            self_repair.maybe_enqueue(pid, failure, health)
        except Exception:
            logger.exception("self-repair enqueue failed for provider %s", pid)


def record_provider_success_event(supervisor: ProviderSupervisor | None, pid: str) -> None:
    """Record one provider success; a storage fault is logged, not fatal.

    The attempt genuinely succeeded, so the run continues with its result;
    later events rebuild the health picture once the store is writable.
    """
    if supervisor is None:
        return
    try:
        supervisor.record_success(pid)
    except HealthStoreError:
        logger.exception("provider health success record failed for %s", pid)
