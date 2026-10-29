"""Durable task admission and immutable authorization snapshots.

Execution and recovery are owned by the operation reducer and kernel driver.
"""
from __future__ import annotations

from typing import Any

from codey.durable.model import thaw_json
from codey.durable.session import DurableSession
from codey.runtime.write.mutation_line import RuntimeMutationLine


def durable_task_for(value: Any, task_id: str) -> DurableSession | None:
    """Return durable authority only when this run was actually admitted."""
    if not isinstance(value, DurableSession) or not task_id:
        return None
    try:
        value.task(task_id)
    except (KeyError, ValueError, TypeError):
        return None
    return value


def record_entry_policy(
    mutations: Any,
    *,
    session_id: str,
    run_id: str,
    policy: Any,
    durable: DurableSession | None = None,
) -> None:
    """Record immutable authorization with the operation checkpoint."""
    if policy is None:
        return
    try:
        payload = policy.to_payload() if hasattr(policy, "to_payload") else dict[str, object]()
    except Exception as exc:
        raise RuntimeError(f"task policy serialization failed: {exc}") from exc
    if not isinstance(mutations, RuntimeMutationLine) or durable is None or mutations.durable is not durable:
        raise RuntimeError("task authorization requires its admitted durable mutation line")
    mutations.set_task_policy(session_id, run_id, policy=payload)


def policy_from_durable_task(
    durable: DurableSession | None, *, task_id: str,
) -> Any:
    """Restore the immutable authorization snapshot from one durable task."""
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.policies.task_policy import TaskPolicy

    if durable is None or not task_id:
        raise RecoveryFailed("durable task policy recovery requires an admitted task")
    try:
        task = durable.task(task_id)
        state = thaw_json(task.state)
        payload = state.get("task_policy") if isinstance(state, dict) else None
        if not isinstance(payload, dict) or not payload:
            raise ValueError("durable task authorization snapshot is missing")
        return TaskPolicy.from_payload(payload)
    except Exception as exc:
        raise RecoveryFailed(f"durable task policy recovery failed: {exc}") from exc
