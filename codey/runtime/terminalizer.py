"""Terminal event projection helpers.

Runtime completion is a projection of already-observed facts.  This module
keeps the terminal payload shape in one place and deliberately does not own
provider, Ghost, ledger, or HTTP concerns.
"""

from __future__ import annotations

from typing import Any

from codey.runtime.outcome import OperationOutcome


def nonnegative_event_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    return 0


def terminal_turns(
    work: object | None,
    *,
    turns: object = None,
    max_turns: int = 0,
) -> int:
    candidates = [nonnegative_event_count(turns)]
    if work is not None:
        candidates.append(nonnegative_event_count(getattr(work, "turns_observed", 0)))
        operation = getattr(work, "operation", None)
        if operation is not None:
            candidates.append(nonnegative_event_count(getattr(operation, "turns_used", 0)))
    used = max(candidates)
    return min(max(0, max_turns), used) if max_turns > 0 else used


def task_done_event(
    *,
    run_id: str,
    session_id: str,
    summary: str,
    stop_reason: str,
    max_turns: int,
    provider: str,
    mode: str,
    work: object | None = None,
    turns: object = None,
    provider_failure: dict[str, Any] | None = None,
    changed: bool | None = None,
    receipt: dict[str, Any] | None = None,
    changes: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "task_done",
        "run_id": run_id,
        "session_id": session_id,
        "summary": summary,
        "stop_reason": stop_reason,
        "turns": terminal_turns(work, turns=turns, max_turns=max_turns),
        "max_turns": max_turns,
        "provider": provider,
        "mode": mode,
    }
    if provider_failure is not None or stop_reason in {"stopped", "error"}:
        event["provider_failure"] = provider_failure
    if changed is not None:
        event["changed"] = changed
    if receipt is not None:
        event["receipt"] = receipt
    if changes is not None:
        event["changes"] = changes
    if research is not None:
        event["research"] = research
    return event


def operation_outcome_from_task_done_event(event: dict[str, object]) -> OperationOutcome:
    """Project a user-visible task terminal event into a runtime outcome."""

    reason = str(event.get("stop_reason") or "").strip() or "done"
    if reason == "done":
        return OperationOutcome.completed(summary="task_done")
    if reason == "stopped":
        return OperationOutcome.aborted(reason="stopped")
    if reason == "approval":
        return OperationOutcome.suspended(reason="approval")
    return OperationOutcome.failed(reason=reason, summary="task_not_done")
