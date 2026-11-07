"""Read-only lifecycle versions from the existing durable task checkpoint."""
from __future__ import annotations

from typing import Literal, TypedDict

from codey.durable.session import DurableSession
from codey.durable.task_projection import task_lifecycle
from codey.types.json_value import JsonObject, JsonValue


class TaskLifecyclePayload(TypedDict, total=False):
    run_id: str
    active_runs: list[JsonObject]
    shell_approvals: list[JsonObject]
    last_terminal_event: JsonObject | None
    last_shell_result: JsonObject | None
    pending_event: JsonObject | None


def version_task_event(durable: DurableSession, event: JsonObject) -> JsonObject:
    """Capture once, before publication/storage; replay never reads a new version."""
    payload = dict(event)
    if payload.get("type") not in {"task_start", "task_done"} or "task_revision" in payload:
        return payload
    try:
        checkpoint = durable.task(str(payload.get("run_id") or ""))
    except (KeyError, AttributeError):
        return payload  # Non-durable test/headless adapters have no checkpoint.
    payload.update(task_revision=checkpoint.revision, task_status=str(checkpoint.status))
    return payload


def snapshot_tasks(durable: DurableSession, payload: TaskLifecyclePayload) -> list[JsonValue]:
    events: tuple[tuple[Literal["last_terminal_event", "last_shell_result", "pending_event"], JsonObject | None], ...] = (
        ("last_terminal_event", payload.get("last_terminal_event")),
        ("last_shell_result", payload.get("last_shell_result")),
        ("pending_event", payload.get("pending_event")),
    )
    ids = {str(row.get("run_id") or "") for row in payload.get("active_runs", ())}
    ids.add(payload.get("run_id") or "")
    for row in payload.get("shell_approvals", ()):
        ids.add(str(row.get("source_run_id") or row.get("run_id") or ""))
        ids.add(str(row.get("continuation_run_id") or ""))
    for _, event in events:
        ids.add(str((event or {}).get("run_id") or ""))
    rows: list[JsonValue] = []
    inactive: set[str] = set()
    for task_id in sorted(ids - {""}):
        try:
            checkpoint = durable.task(task_id)
            input_id = checkpoint.state.get("input_entry_id")
            if input_id and input_id not in {entry.entry_id for entry in durable.context(checkpoint.conversation_id)}:
                inactive.add(task_id)
                continue
            rows.append(task_lifecycle(checkpoint))
        except (KeyError, AttributeError):
            continue
    for key, event in events:
        if (event or {}).get("run_id") in inactive:
            payload[key] = None
    if inactive:
        payload["shell_approvals"] = [row for row in payload.get("shell_approvals", ())
                                      if (row.get("source_run_id") or row.get("run_id")) not in inactive]
    return rows
